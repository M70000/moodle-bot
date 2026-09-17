"""Módulo de coleta de cursos, materiais e atividades do Moodle / UFMG Virtual.

Utiliza a sessão autenticada do Playwright para sincronizar arquivos didáticos
e monitorar prazos de entrega.
"""

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rich.console import Console
from rich.table import Table

from config.settings import settings
from src.auth.moodle_auth import MoodleAuth

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


def sanitize_filename(name: str) -> str:
    """Remove caracteres inválidos para nomes de arquivos no sistema operacional."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:100] if len(cleaned) > 100 else cleaned


class Course(BaseModel):
    """Representa uma disciplina no Moodle/UFMG Virtual."""
    id: str
    name: str
    url: str
    semester: Optional[str] = None

    @property
    def safe_name(self) -> str:
        return sanitize_filename(self.name)


class CourseMaterial(BaseModel):
    """Representa um material de apoio baixado da disciplina (slides, PDFs, listas)."""
    id: str
    course_id: str
    title: str
    url: str
    filename: str
    local_path: Optional[Path] = None
    material_type: str = "resource"


MONTHS_PT = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "março": 3, "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}


def parse_moodle_date(date_val: Any) -> Optional[datetime]:
    """Converte strings de datas do Moodle em objetos datetime estruturados."""
    if not date_val:
        return None
    if isinstance(date_val, datetime):
        return date_val
    if not isinstance(date_val, str):
        return None

    s = date_val.strip().lower()
    if s in ["sem prazo", "não especificado", "nao especificado", "n/a", "none", "sob demanda"]:
        return None

    # Padrão 1: '7 de janeiro de 2025' ou 'terça-feira, 7 de janeiro de 2025, 23:59' ou '15 de dez. de 2024 às 18:00'
    m1 = re.search(r'(\d{1,2})\s+de\s+([a-zçã]+)\.?\s+de\s+(\d{4})(?:[,\s]+(?:às\s+)?(\d{1,2}):(\d{2}))?', s)
    if m1:
        day = int(m1.group(1))
        m_name = m1.group(2)
        year = int(m1.group(3))
        hour = int(m1.group(4)) if m1.group(4) is not None else 23
        minute = int(m1.group(5)) if m1.group(5) is not None else 59
        month = MONTHS_PT.get(m_name)
        if month:
            try:
                return datetime(year, month, day, hour, minute)
            except ValueError:
                pass

    # Padrão 2: '10 mar 2025' ou '10 mar. 2025 12:00'
    m2 = re.search(r'(\d{1,2})\s+([a-zçã]+)\.?\s+(\d{4})(?:[,\s]+(?:às\s+)?(\d{1,2}):(\d{2}))?', s)
    if m2:
        day = int(m2.group(1))
        m_name = m2.group(2)
        year = int(m2.group(3))
        hour = int(m2.group(4)) if m2.group(4) is not None else 23
        minute = int(m2.group(5)) if m2.group(5) is not None else 59
        month = MONTHS_PT.get(m_name)
        if month:
            try:
                return datetime(year, month, day, hour, minute)
            except ValueError:
                pass

    # Padrão 3: '07/01/2025 23:59' ou '07/01/2025'
    m3 = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})(?:[,\s]+(?:às\s+)?(\d{1,2}):(\d{2}))?', s)
    if m3:
        day, month, year = int(m3.group(1)), int(m3.group(2)), int(m3.group(3))
        hour = int(m3.group(4)) if m3.group(4) is not None else 23
        minute = int(m3.group(5)) if m3.group(5) is not None else 59
        try:
            return datetime(year, month, day, hour, minute)
        except ValueError:
            pass

    # Padrão 4: ISO '2025-01-07' ou '2025-01-07T23:59:00'
    m4 = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:[t\s]+(\d{1,2}):(\d{2}))?', s)
    if m4:
        year, month, day = int(m4.group(1)), int(m4.group(2)), int(m4.group(3))
        hour = int(m4.group(4)) if m4.group(4) is not None else 23
        minute = int(m4.group(5)) if m4.group(5) is not None else 59
        try:
            return datetime(year, month, day, hour, minute)
        except ValueError:
            pass

    return None


class Assignment(BaseModel):
    """Representa uma atividade/tarefa acadêmica com prazo de entrega."""
    model_config = ConfigDict(extra="allow")

    id: str
    course_id: str = ""
    course_name: str
    title: str
    url: str = ""
    description: str = ""
    due_date_str: Optional[str] = None
    due_date: Optional[datetime] = None
    time_remaining: Optional[str] = None
    submission_status: str = "Não enviado"
    grading_status: str = "Não avaliado"
    grade_value: Optional[str] = None
    feedback_comments: Optional[str] = None
    graded_by: Optional[str] = None
    graded_date: Optional[str] = None
    activity_type: str = "assign"
    submitted_files: List[str] = Field(default_factory=list)
    can_submit: bool = True
    attachments: List[CourseMaterial] = Field(default_factory=list)
    platform: str = "moodle"
    submission_types: List[str] = Field(default_factory=list)
    is_coding_task: bool = False

    @model_validator(mode="after")
    def _auto_parse_due_date(self) -> "Assignment":
        if self.due_date is None and self.due_date_str:
            self.due_date = parse_moodle_date(self.due_date_str)
        return self

    @property
    def course(self) -> str:
        """Compatibilidade para acesso como course ou course_name."""
        return self.course_name

    @property
    def status_text(self) -> str:
        """Texto descritivo do status atual da atividade."""
        if self.is_submitted:
            return "Concluído"
        if self.is_expired:
            return "Prazo expirado"
        return self.submission_status or "Pendente"

    @property
    def has_grade(self) -> bool:
        """Verifica se o professor já publicou uma nota para a tarefa."""
        if self.grade_value and str(self.grade_value).strip() and str(self.grade_value).strip().lower() not in ("none", "null", "-") and "não" not in str(self.grade_value).lower() and "nao" not in str(self.grade_value).lower():
            return True
        g_lower = (self.grading_status or "").lower().strip()
        is_neg = not g_lower or any(neg in g_lower for neg in ["não", "nao", "not", "sem nota", "nenhuma nota", "\ufffd"]) or bool(re.search(r"\bn[aã\W_]*o\s*(?:avaliad|graded)", g_lower))
        if not is_neg:
            if any(g in g_lower for g in ["avaliado", "graded"]):
                return True
        return False

    @property
    def is_submitted(self) -> bool:
        """Verifica se a atividade já foi submetida ou concluída pelo aluno."""
        extra = getattr(self, "__pydantic_extra__", {}) or {}
        if extra.get("is_submitted") is True:
            return True

        if self.has_grade:
            return True

        status_lower = (self.submission_status or "").lower().strip()
        time_lower = (self.time_remaining or "").lower()

        is_neg = not status_lower or any(neg in status_lower for neg in ["não", "nao", "not", "unsubmitted", "sem envio", "nenhum envio", "\ufffd"]) or bool(re.search(r"\bn[aã\W_]*o\s*(?:enviad|submetid|avaliad)", status_lower))
        if not is_neg:
            if any(term in status_lower for term in [
                "enviado", "submetido", "submitted", "avaliado", "graded",
                "concluído", "concluido", "feito", "finalizada", "finalizado", "entregue"
            ]):
                return True

        if "enviada" in time_lower and "adiantado" in time_lower:
            return True
        if len(self.submitted_files) > 0:
            return True

        return False

    @property
    def is_expired(self) -> bool:
        """Verifica se o prazo da tarefa já expirou no passado."""
        time_lower = (self.time_remaining or "").lower()
        if any(term in time_lower for term in ["atrasad", "expirad", "encerrad", "fechad"]):
            return True
        if self.due_date and self.due_date < datetime.now():
            return True
        return False

    @property
    def has_online_submission(self) -> bool:
        """Indica se a atividade aceita submissão online ou se é apenas leitura/em papel ('em branco')."""
        if getattr(self, "activity_type", "") == "quiz":
            return True
        sub_types = getattr(self, "submission_types", None) or []
        if not sub_types:
            if getattr(self, "platform", "") == "canvas":
                return False
            return True
        non_submittable = {"none", "on_paper", "not_graded"}
        return not set(sub_types).issubset(non_submittable)

    @property
    def is_actionable_pending(self) -> bool:
        """Indica se a tarefa é realmente pendente e precisa de resolução pela IA."""
        if self.is_submitted:
            return False
        if self.is_expired:
            return False
        if not self.has_online_submission:
            return False
        return True


class CourseAnnouncement(BaseModel):
    """Representa um comunicado ou aviso publicado pelo professor no fórum da disciplina."""
    id: str
    course_id: str
    course_name: str
    title: str
    author: str = ""
    date: str = ""
    url: str
    message: str = ""


class MoodleScraper:
    """Scraper automatizado para Moodle UFMG Virtual."""

    def __init__(self, auth: Optional[MoodleAuth] = None):
        self.auth = auth or MoodleAuth()
        self.base_url = settings.MOODLE_BASE_URL.rstrip("/")
        self.materials_dir = settings.STORAGE_MATERIALS_DIR

    async def list_courses(self, semester_prefix: Optional[str] = None) -> List[Course]:
        """Lista as disciplinas do aluno a partir da página Minhas Turmas.

        Se semester_prefix for fornecido (ex: '2026_2'), filtra por esse semestre.
        Caso contrário, busca o semestre mais recente encontrado.
        """
        if not self.auth.session_exists:
            raise RuntimeError("Sessão não encontrada. Execute 'python -m src.auth.moodle_auth' primeiro.")

        courses: List[Course] = []

        async with async_playwright() as p:
            browser, context = await self.auth.get_authenticated_context(p, headless=True)
            try:
                page = await context.new_page()
                await page.goto(f"{self.base_url}/minhasturmas", wait_until="domcontentloaded")

                raw_courses = await page.evaluate('''() => {
                    return Array.from(document.querySelectorAll("a"))
                        .filter(a => a.href && (a.href.includes("course/view.php") || a.href.includes("/turma/")))
                        .map(a => {
                            const match = a.href.match(/id=(\\d+)/);
                            return {
                                id: match ? match[1] : a.href,
                                name: a.innerText.trim(),
                                url: a.href
                            };
                        }).filter(c => c.name.length > 3);
                }''')

                semesters_found = set()
                for c in raw_courses:
                    # Tenta detectar semestre pelo nome (ex: 2026_2, 2026_1)
                    sem_match = re.search(r"(\d{4}_\d)", c["name"])
                    sem = sem_match.group(1) if sem_match else None
                    if sem:
                        semesters_found.add(sem)

                    course_obj = Course(
                        id=c["id"],
                        name=c["name"],
                        url=c["url"],
                        semester=sem
                    )
                    courses.append(course_obj)

                # Define o semestre alvo
                target_semester = semester_prefix
                if not target_semester and semesters_found:
                    target_semester = sorted(list(semesters_found), reverse=True)[0]

                if target_semester:
                    filtered = [c for c in courses if c.semester == target_semester]
                    return filtered if filtered else courses

                return courses

            finally:
                await browser.close()

    async def sync_course_materials(
        self,
        course: Course,
        context: BrowserContext,
        page: Page
    ) -> List[CourseMaterial]:
        """Varre e baixa novos materiais (slides, apostilas, PDFs) de uma disciplina."""
        console.print(f"[cyan]Verificando materiais de: [bold]{course.name}[/bold]...[/cyan]")
        course_dir = self.materials_dir / course.safe_name
        course_dir.mkdir(parents=True, exist_ok=True)

        # Fast-cache: se a disciplina já possui materiais baixados localmente, aproveita o disco instantaneamente
        if course_dir.exists():
            disk_files = [p for p in course_dir.iterdir() if p.is_file() and p.stat().st_size > 0 and not p.name.startswith(".")]
            if len(disk_files) >= 5:
                return [
                    CourseMaterial(
                        id=p.name,
                        course_id=course.id,
                        title=p.stem,
                        url=course.url,
                        filename=p.name,
                        local_path=p,
                        material_type="resource"
                    )
                    for p in disk_files
                ]

        materials: List[CourseMaterial] = []

        try:
            await page.goto(course.url, wait_until="domcontentloaded", timeout=25000)

            # Extrai links de materiais didáticos da página
            resources_data = await page.evaluate('''() => {
                const items = [];
                // Seleciona recursos didáticos do Moodle
                document.querySelectorAll(".activity.resource, li.modtype_resource, .activity.folder, li.modtype_folder, a[href*='mod/resource/view.php']").forEach(el => {
                    const a = el.tagName === 'A' ? el : el.querySelector("a");
                    const nameEl = el.querySelector(".instancename, .activityname");
                    if (a && a.href) {
                        let title = nameEl ? nameEl.innerText.trim() : a.innerText.trim();
                        // Remove sufixos como 'Arquivo', 'Documento' adicionados por leitores de tela
                        title = title.replace(/(Arquivo|Documento|Folder|Recurso)$/i, "").trim();
                        const idMatch = a.href.match(/id=(\\d+)/);
                        items.push({
                            id: idMatch ? idMatch[1] : a.href,
                            title: title,
                            url: a.href,
                            type: el.className.includes("folder") ? "folder" : "resource"
                        });
                    }
                });
                return items;
            }''')

            # Mapeia arquivos já existentes no disco para evitar requisições lentas desnecessárias
            existing_files_map = {}
            if course_dir.exists():
                for p in course_dir.iterdir():
                    if p.is_file() and p.stat().st_size > 0:
                        existing_files_map[p.stem.lower()] = p

            # Para cada recurso, faz o download apenas se ainda não existir localmente
            for res in resources_data:
                res_url = res["url"]
                res_title = res["title"]
                safe_title = sanitize_filename(res_title)

                # Fast-path: se o arquivo já foi baixado anteriormente, reutiliza imediatamente
                if safe_title.lower() in existing_files_map:
                    dest_file = existing_files_map[safe_title.lower()]
                    materials.append(
                        CourseMaterial(
                            id=res["id"],
                            course_id=course.id,
                            title=res_title,
                            url=res_url,
                            filename=dest_file.name,
                            local_path=dest_file,
                            material_type=res["type"]
                        )
                    )
                    continue

                try:
                    # Executa requisição com timeout de 5 segundos
                    resp = await context.request.get(res_url, timeout=5000)
                    if resp.status == 200:
                        content_disposition = resp.headers.get("content-disposition", "")
                        content_type = resp.headers.get("content-type", "")

                        # Determina nome do arquivo
                        filename = None
                        if "filename=" in content_disposition:
                            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
                            if match:
                                filename = unquote(match.group(1))

                        if not filename:
                            # Se não veio no header, deduz pela extensão do content-type ou título
                            ext = ".pdf" if "pdf" in content_type else ".bin"
                            filename = f"{safe_title}{ext}"
                        else:
                            filename = sanitize_filename(filename)

                        # Evita salvar páginas HTML genéricas que não são arquivos de estudo
                        if "text/html" in content_type and not filename.endswith(".html"):
                            continue

                        dest_file = course_dir / filename
                        # Baixa apenas se o arquivo ainda não existir localmente
                        if not dest_file.exists() or dest_file.stat().st_size == 0:
                            body = await resp.body()
                            dest_file.write_bytes(body)
                            console.print(f"  [green]+ Novo material baixado:[/green] {filename} ({len(body) // 1024} KB)")

                        materials.append(
                            CourseMaterial(
                                id=res["id"],
                                course_id=course.id,
                                title=res_title,
                                url=res_url,
                                filename=filename,
                                local_path=dest_file,
                                material_type=res["type"]
                            )
                        )
                except Exception as e:
                    console.print(f"  [yellow]Não foi possível baixar material '{res_title}': {e}[/yellow]")

        except Exception as e:
            console.print(f"[red]Erro ao varrer materiais do curso {course.name}: {e}[/red]")

        return materials

    async def get_course_assignments(
        self,
        course: Course,
        context: BrowserContext,
        page: Page
    ) -> List[Assignment]:
        """Varre e extrai tarefas pendentes (mod/assign) de uma disciplina."""
        assignments: List[Assignment] = []
        course_assign_dir = self.materials_dir / course.safe_name / "assignments"
        course_assign_dir.mkdir(parents=True, exist_ok=True)

        try:
            await page.goto(course.url, wait_until="domcontentloaded", timeout=25000)

            # Localiza links de tarefas (assign) e questionários (quiz)
            activity_links = await page.evaluate('''() => {
                const list = [];
                // 1. Tarefas tradicionais (assign)
                document.querySelectorAll(".activity.assign, li.modtype_assign, a[href*='mod/assign/view.php']").forEach(el => {
                    const a = el.tagName === 'A' ? el : el.querySelector("a");
                    const nameEl = el.querySelector(".instancename, .activityname");
                    if (a && a.href && a.href.includes("mod/assign/view.php")) {
                        const title = nameEl ? nameEl.innerText.trim() : a.innerText.trim();
                        const idMatch = a.href.match(/id=(\\d+)/);
                        list.push({
                            id: idMatch ? idMatch[1] : a.href,
                            type: "assign",
                            title: title.replace(/(Tarefa|Atividade)$/i, "").trim(),
                            url: a.href,
                            status: null,
                            dueDate: null
                        });
                    }
                });

                // 2. Questionários e Quizzes avaliativos (quiz)
                document.querySelectorAll("a[href*='mod/quiz/view.php']").forEach(a => {
                    const container = a.closest("li, div.activity, div.activity-item, div.progressEventInfo") || a.parentElement;
                    const containerText = container ? container.innerText.replace(/\\s+/g, ' ').trim() : '';

                    let status = "Pendente";
                    if (containerText.includes("Concluído") || containerText.includes("Feito")) {
                        status = "Concluído";
                    } else if (containerText.includes("Não concluído") || containerText.includes("A fazer")) {
                        status = "A fazer";
                    }

                    const dueMatch = containerText.match(/Prevista:\\s*([0-9a-zA-Z\\sºde]+)/i);
                    const idMatch = a.href.match(/id=(\\d+)/);
                    if (a.innerText.trim()) {
                        list.push({
                            id: idMatch ? idMatch[1] : a.href,
                            type: "quiz",
                            title: a.innerText.trim(),
                            url: a.href,
                            status: status,
                            dueDate: dueMatch ? dueMatch[1].trim() : null
                        });
                    }
                });

                return list;
            }''')

            # Deduplica por ID
            seen_activity_ids = set()
            filtered_activities = []
            for item in activity_links:
                if item["id"] not in seen_activity_ids and item["title"]:
                    seen_activity_ids.add(item["id"])
                    filtered_activities.append(item)

            # Processa cada atividade encontrada
            for item in filtered_activities:
                if item["type"] == "quiz":
                    is_concluido = item.get("status") == "Concluído"
                    quiz_assignment = Assignment(
                        id=item["id"],
                        course_id=course.id,
                        course_name=course.name,
                        title=item["title"],
                        url=item["url"],
                        description=f"Questionário/Quiz avaliativo da disciplina {course.name}",
                        activity_type="quiz",
                        due_date_str=item.get("dueDate"),
                        submission_status="Concluído" if is_concluido else "A fazer (Pendente)",
                        grading_status="Avaliado" if is_concluido else "Não avaliado",
                        can_submit=not is_concluido
                    )
                    assignments.append(quiz_assignment)
                    continue

                # Processa tarefa tradicional (assign)
                assign_url = item["url"]
                try:
                    await page.goto(assign_url, wait_until="domcontentloaded", timeout=25000)

                    data = await page.evaluate('''() => {
                        const intro = document.querySelector(".activity-description, #intro, .box.generalbox");
                        const tables = Array.from(document.querySelectorAll("table.generaltable tr")).map(tr => {
                            const th = tr.querySelector("th, td.cell.c0");
                            const td = tr.querySelector("td.lastcol, td.cell.c1");
                            return {
                                label: th ? th.innerText.trim() : "",
                                value: td ? td.innerText.trim() : ""
                            };
                        }).filter(x => x.label);

                        // Botões de ação de envio
                        const buttonsText = Array.from(document.querySelectorAll("button, input[type=submit], a.btn")).map(b => b.innerText || b.value || "");
                        const hasEditOrRemove = buttonsText.some(t => t.includes("Editar envio") || t.includes("Remover envio"));
                        const hasAddSubmission = buttonsText.some(t => t.includes("Adicionar envio"));

                        // Arquivos fornecidos pelo professor (enunciado, dados)
                        const introLinks = Array.from(document.querySelectorAll(".activity-description a, #intro a, .introattachments a, a[href*='introattachment']")).filter(a => {
                            return a.href && (a.href.includes("pluginfile.php") || a.href.endsWith(".pdf") || a.href.endsWith(".csv") || a.href.endsWith(".docx"));
                        }).map(a => ({ name: a.innerText.trim(), href: a.href }));

                        // Arquivos já submetidos pelo aluno
                        const submittedLinks = [];
                        document.querySelectorAll("a[href*='assignsubmission_file']").forEach(a => {
                            const t = a.innerText.trim();
                            if (t && !submittedLinks.includes(t)) submittedLinks.push(t);
                        });
                        document.querySelectorAll("table.generaltable tr").forEach(tr => {
                            const th = tr.querySelector("th, td.cell.c0");
                            if (th && th.innerText.includes("Envios de arquivo")) {
                                tr.querySelectorAll("a").forEach(a => {
                                    const t = a.innerText.trim();
                                    if (t && !submittedLinks.includes(t)) submittedLinks.push(t);
                                });
                            }
                        });

                        return {
                            introText: intro ? intro.innerText.trim() : "",
                            table: tables,
                            introFiles: introLinks,
                            submittedFiles: submittedLinks,
                            hasEditOrRemove: hasEditOrRemove,
                            hasAddSubmission: hasAddSubmission
                        };
                    }''')

                    status_map: Dict[str, str] = {}
                    for row in data.get("table", []):
                        status_map[row["label"]] = row["value"]

                    sub_status = status_map.get("Status de envio", "Não enviado")
                    due_date_str = status_map.get("Data de entrega", None)
                    time_rem = status_map.get("Tempo restante", None)
                    grade_status = status_map.get("Status da avaliação", "Não avaliado")
                    grade_val = status_map.get("Nota", None) or status_map.get("Grade", None)
                    feedback_com = status_map.get("Comentários sobre o feedback", None) or status_map.get("Feedback", None)
                    graded_who = status_map.get("Avaliado por", None)
                    graded_when = status_map.get("Avaliado em", None)
                    submitted_files_list = data.get("submittedFiles", [])

                    if data.get("hasEditOrRemove") and "enviado" not in sub_status.lower():
                        sub_status = "Enviado para avaliação"

                    # Baixa arquivos anexos da tarefa (instruções, CSVs, enunciados)
                    assign_folder = course_assign_dir / sanitize_filename(item["title"])
                    assign_folder.mkdir(parents=True, exist_ok=True)
                    attachments: List[CourseMaterial] = []

                    for f in data.get("introFiles", []):
                        f_url = f["href"]
                        f_name = sanitize_filename(f["name"] or "anexo")
                        if not any(f_name.endswith(ext) for ext in [".pdf", ".csv", ".docx", ".zip", ".txt", ".r"]):
                            f_name += ".pdf"

                        f_path = assign_folder / f_name
                        try:
                            if not f_path.exists():
                                f_resp = await context.request.get(f_url)
                                if f_resp.status == 200:
                                    f_path.write_bytes(await f_resp.body())
                        except Exception:
                            pass

                        attachments.append(
                            CourseMaterial(
                                id=f_name,
                                course_id=course.id,
                                title=f["name"],
                                url=f_url,
                                filename=f_name,
                                local_path=f_path,
                                material_type="assignment_attachment"
                            )
                        )

                    assignment = Assignment(
                        id=item["id"],
                        course_id=course.id,
                        course_name=course.name,
                        title=item["title"],
                        url=assign_url,
                        description=data.get("introText", ""),
                        due_date_str=due_date_str,
                        time_remaining=time_rem,
                        submission_status=sub_status,
                        grading_status=grade_status,
                        grade_value=grade_val,
                        feedback_comments=feedback_com,
                        graded_by=graded_who,
                        graded_date=graded_when,
                        submitted_files=submitted_files_list,
                        can_submit=data.get("hasAddSubmission", True) and not data.get("hasEditOrRemove", False),
                        attachments=attachments
                    )
                    assignments.append(assignment)

                except Exception as e:
                    console.print(f"  [yellow]Erro ao extrair detalhes da tarefa '{item['title']}': {e}[/yellow]")

        except Exception as e:
            console.print(f"[red]Erro ao varrer tarefas do curso {course.name}: {e}[/red]")

        return assignments

    async def get_course_announcements(
        self,
        course: Course,
        context: BrowserContext,
        page: Page,
        known_ids: Optional[set] = None
    ) -> List[CourseAnnouncement]:
        """Varre o fórum de 'Avisos' da turma e retorna comunicados publicados pelo professor."""
        announcements: List[CourseAnnouncement] = []
        try:
            await page.goto(course.url, wait_until="domcontentloaded", timeout=25000)

            # Localiza links para fóruns de notícias/avisos
            forum_links = await page.evaluate('''() => {
                const list = [];
                document.querySelectorAll("a[href*='mod/forum/view.php']").forEach(a => {
                    const title = a.innerText.trim();
                    list.push({
                        title: title,
                        url: a.href
                    });
                });
                return list;
            }''')

            target_forums = [
                f for f in forum_links
                if any(k in f["title"].lower() for k in ["aviso", "notícia", "noticia", "comunicado", "mural", "geral"])
            ]
            if not target_forums and forum_links:
                target_forums = [forum_links[0]]

            for forum in target_forums:
                await page.goto(forum["url"], wait_until="domcontentloaded", timeout=25000)

                # Extrai as discussões da tabela
                discussions_data = await page.evaluate('''() => {
                    const items = [];
                    document.querySelectorAll("table.forumheaderlist tr.discussion, .discussion-list tr, table.discussion-list tr, .forumpost").forEach(el => {
                        const link = el.querySelector("th a, td.topic a, a.discussion-title, a[href*='discuss.php?d=']");
                        const author = el.querySelector(".author, td.author, td.starter, .user-name");
                        const date = el.querySelector(".lastpost, td.lastpost, .time, .post-date, td.created");
                        if (link && link.href && link.href.includes("discuss.php?d=")) {
                            const dMatch = link.href.match(/d=(\\d+)/);
                            if (dMatch) {
                                items.push({
                                    id: dMatch[1],
                                    title: link.innerText.trim(),
                                    url: link.href,
                                    author: author ? author.innerText.trim().replace(/\\s+/g, ' ') : "",
                                    date: date ? date.innerText.trim().replace(/\\s+/g, ' ') : ""
                                });
                            }
                        }
                    });
                    return items;
                }''')

                for d in discussions_data:
                    d_id = d["id"]
                    if known_ids and d_id in known_ids:
                        announcements.append(CourseAnnouncement(
                            id=d_id,
                            course_id=course.id,
                            course_name=course.name,
                            title=d["title"],
                            author=d.get("author", "Professor"),
                            date=d.get("date", ""),
                            url=d["url"],
                            message=""
                        ))
                        continue

                    try:
                        await page.goto(d["url"], wait_until="networkidle", timeout=15000)
                        post_info = await page.evaluate('''() => {
                            const post = document.querySelector("article.forumpost, div.forumpost");
                            const authorEl = document.querySelector(".header a[href*='user/view.php'], .author, [data-region='author-name']");
                            const timeEl = document.querySelector("time, .time");
                            const contentEl = document.querySelector(".post-content-container, .post-message, .content, .message");
                            const mainEl = document.querySelector("#region-main, [role='main']");

                            return {
                                author: authorEl ? authorEl.innerText.trim() : "",
                                time: timeEl ? timeEl.innerText.trim() : "",
                                message: contentEl ? contentEl.innerText.trim() : (mainEl ? mainEl.innerText.trim() : "")
                            };
                        }''')

                        author_final = post_info.get("author") or d.get("author") or "Professor"
                        date_final = post_info.get("time") or d.get("date") or ""
                        msg_final = post_info.get("message") or ""

                        msg_final = re.sub(r"(?i)Link direto.*", "", msg_final).strip()
                        msg_final = re.sub(r"(?i)Responder.*", "", msg_final).strip()

                        ann = CourseAnnouncement(
                            id=d_id,
                            course_id=course.id,
                            course_name=course.name,
                            title=d["title"],
                            author=author_final,
                            date=date_final,
                            url=d["url"],
                            message=msg_final
                        )
                        announcements.append(ann)
                    except Exception as err:
                        console.print(f"  [yellow]Erro ao ler aviso '{d['title']}': {err}[/yellow]")
                        announcements.append(CourseAnnouncement(
                            id=d_id,
                            course_id=course.id,
                            course_name=course.name,
                            title=d["title"],
                            author=d.get("author", "Professor"),
                            date=d.get("date", ""),
                            url=d["url"],
                            message=""
                        ))

        except Exception as e:
            console.print(f"[yellow]Não foi possível verificar avisos em {course.name}: {e}[/yellow]")

        return announcements

    async def scan_all(
        self,
        semester_prefix: Optional[str] = None,
        sync_materials: bool = True,
        sync_announcements: bool = True,
        known_announcement_ids: Optional[set] = None
    ) -> Tuple[List[Course], List[Assignment], List[CourseAnnouncement]]:
        """Executa a varredura completa de todas as disciplinas ativas."""
        courses = await self.list_courses(semester_prefix=semester_prefix)
        all_assignments: List[Assignment] = []
        all_announcements: List[CourseAnnouncement] = []

        console.print(f"[bold green]Disciplinas localizadas no semestre ({len(courses)}):[/bold green]")
        for c in courses:
            console.print(f" • [cyan]{c.name}[/cyan]")

        async with async_playwright() as p:
            browser, context = await self.auth.get_authenticated_context(p, headless=True)
            try:
                page = await context.new_page()

                for course in courses:
                    console.rule(f"[bold yellow]{course.name}[/bold yellow]")

                    # 1. Sincroniza materiais da disciplina
                    if sync_materials:
                        materials = await self.sync_course_materials(course, context, page)
                        console.print(f"  → Total de materiais catalogados: [bold]{len(materials)}[/bold]")

                    # 2. Coleta tarefas da disciplina
                    assignments = await self.get_course_assignments(course, context, page)
                    console.print(f"  → Tarefas encontradas: [bold]{len(assignments)}[/bold]")
                    all_assignments.extend(assignments)

                    # 3. Coleta comunicados e avisos da turma
                    if sync_announcements:
                        announcements = await self.get_course_announcements(
                            course, context, page, known_ids=known_announcement_ids
                        )
                        if announcements:
                            console.print(f"  → Avisos catalogados: [bold]{len(announcements)}[/bold]")
                            all_announcements.extend(announcements)

            finally:
                await browser.close()

        return courses, all_assignments, all_announcements


async def main():
    """CLI para teste de varredura e download do Moodle Scraper."""
    parser = argparse.ArgumentParser(description="Moodle Scraper - UFMG Virtual")
    parser.add_argument("--semester", default=None, help="Prefixo do semestre (ex: 2026_2)")
    parser.add_argument("--no-materials", action="store_true", help="Não baixar materiais, apenas listar tarefas")
    args = parser.parse_args()

    scraper = MoodleScraper()
    courses, assignments, announcements = await scraper.scan_all(
        semester_prefix=args.semester,
        sync_materials=not args.no_materials
    )

    console.print("\n")
    table = Table(title="Resumo das Tarefas Encontradas")
    table.add_column("Disciplina", style="cyan")
    table.add_column("Tarefa", style="white")
    table.add_column("Status de Envio", style="yellow")
    table.add_column("Prazo / Tempo Restante", style="green")

    for a in assignments:
        status_color = "green" if a.is_submitted else "bold red"
        table.add_row(
            a.course_name,
            a.title,
            f"[{status_color}]{a.submission_status}[/{status_color}]",
            f"{a.due_date_str or 'Sem prazo'} ({a.time_remaining or 'N/A'})"
        )

    console.print(table)


if __name__ == "__main__":
    asyncio.run(main())
