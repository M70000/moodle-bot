"""Módulo de Notificações, Revisão e Slash Commands via Discord Bot.

Inclui comandos interativos:
  /tarefas              - Lista tarefas pendentes e entregues com prazos
  /materiais            - Envia slides e materiais de estudo de uma disciplina
  /resolver             - Resolve ou refaz tarefas sob demanda com instruções e anexos
  /resolver_lote        - Resolve múltiplas tarefas pendentes em lote com modo configurável
  /status               - Exibe o estado da sessão do Moodle, materiais e IA
  /adicionarconteudo    - Envia novos materiais e resumos salvando na memória da IA
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import re
import unicodedata

import discord
from discord import app_commands, ui
from discord.ext import commands
from rich.console import Console

from config.settings import settings
from src.auth.moodle_auth import MoodleAuth
from src.scheduler.queue_manager import queue_manager, QueueItem, QueueTaskType, QueueTaskStatus
from src.scheduler.state import DaemonState
from src.scraper.moodle_scraper import Assignment, CourseMaterial, sanitize_filename
from src.scraper.moodle_submitter import MoodleSubmitter
from src.solver.gemini_solver import GeminiSolver, SolutionDraft
from src.solver.ai_solver import AISolver, get_active_provider
from src.solver.study_tutor import StudyTutor
from src.notifier.bridge_manager import cloud_bridge

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


class DiscordLiveReporter:
    """Gerencia um feed de log ao vivo editado em tempo real em uma mensagem do Discord."""

    def __init__(self, message: Any, initial_header: str):
        self.message = message
        self.header = initial_header
        self.logs: List[str] = []
        self._last_edit_time = 0.0
        self._edit_throttle_seconds = 1.2
        self._pending_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._is_closed = False

    async def log(self, text: str):
        """Adiciona uma linha de log ao feed e programa a atualização visual da mensagem."""
        if self._is_closed:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        clean_text = text.strip()
        # Remove tags de estilização Rich se presentes
        clean_text = re.sub(r"\[/?(cyan|green|yellow|red|bold|dim|underline)[^\]]*\]", "", clean_text)
        self.logs.append(f"[{timestamp}] {clean_text}")
        if len(self.logs) > 12:
            self.logs = self.logs[-12:]
        console.print(f"[dim cyan]⚡ [Live Log {timestamp}][/dim cyan] {clean_text}")

        now = time.time()
        if now - self._last_edit_time >= self._edit_throttle_seconds:
            await self._flush()
        else:
            if not self._pending_task or self._pending_task.done():
                self._pending_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self):
        await asyncio.sleep(self._edit_throttle_seconds)
        await self._flush()

    async def _flush(self):
        if self._is_closed or not self.message:
            return
        async with self._lock:
            self._last_edit_time = time.time()
            log_block = "\n".join(self.logs)
            content = f"{self.header}\n```bash\n{log_block}\n```"
            try:
                if hasattr(self.message, "edit"):
                    await self.message.edit(content=content)
            except Exception:
                pass

    async def finish(self, final_content: str, embed: Optional[discord.Embed] = None):
        """Finaliza o live reporter, cancela pendências e substitui a mensagem pelo conteúdo final."""
        self._is_closed = True
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()
            try:
                await self._pending_task
            except (asyncio.CancelledError, Exception):
                pass

        async with self._lock:
            # Respeita intervalo de segurança antes do edit final para prevenir 429
            elapsed = time.time() - self._last_edit_time
            if elapsed < 0.6:
                await asyncio.sleep(0.6 - elapsed)

            if self.message and hasattr(self.message, "edit"):
                try:
                    if embed:
                        await self.message.edit(content=final_content, embed=embed)
                    else:
                        await self.message.edit(content=final_content)
                except Exception:
                    pass


def normalize_text(text: Optional[Any]) -> str:
    """Remove acentos e padroniza para buscas tolerantes, imune a valores None."""
    if text is None:
        return ""
    return unicodedata.normalize("NFKD", str(text)).encode("ASCII", "ignore").decode("utf-8").lower()


def clean_display_course(course: str) -> str:
    """Simplifica nomes longos de disciplina para caber elegantemente nas opções do Discord."""
    c = re.sub(r"^\d{4}_\d\s*-\s*", "", course)
    c = re.sub(r"\s*-\s*(T[A-Z0-9]+|METATURMA)$", "", c, flags=re.IGNORECASE).strip()
    return c.title() if c else course


def find_assignment_by_query(query: Optional[str], assignments: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Localiza deterministicamente uma atividade em assignments com máxima precisão e segurança.

    Regras de prioridade:
    1. ID exato no dicionário (ex: '45759').
    2. URL de atividade Moodle (/mod/assign ou /mod/quiz) extraindo o parâmetro ?id=.
    3. URL exata de atividade (contendo /mod/).
    4. NUNCA aceita match de URL base ou genérica (ex: virtual.ufmg.br sem /mod/).
    5. Normalização de query (remove sufixos de autocomplete '(Prazo: ...)', '[Concluído]').
    6. Busca ponderada por título exato, título+disciplina, prefixo, substring e sobreposição de termos.
    """
    if not query or not assignments:
        return None
    raw_query = str(query).strip()

    # 1. Match direto pelo ID exato da atividade
    if raw_query in assignments:
        return assignments[raw_query]

    # 2. Se for URL de atividade do Moodle com parâmetro id=
    if "id=" in raw_query and ("mod/assign" in raw_query or "mod/quiz" in raw_query or "view.php" in raw_query):
        m = re.search(r"[?&]id=(\d+)", raw_query)
        if m:
            extracted_id = m.group(1)
            if extracted_id in assignments:
                return assignments[extracted_id]
            for aid, it in assignments.items():
                if extracted_id == str(it.get("id")):
                    return it

    # 3. Se for URL exata de atividade (/mod/)
    if raw_query.startswith("http") and ("/mod/" in raw_query):
        clean_url = raw_query.split("#")[0].rstrip("/")
        for aid, it in assignments.items():
            if it.get("url", "").split("#")[0].rstrip("/") == clean_url:
                return it

    # SEGURANÇA CRÍTICA: Se for uma URL base ou genérica (sem /mod/), NUNCA tente casar como atividade!
    if raw_query.startswith("http") and "/mod/" not in raw_query:
        return None

    # 4. Limpeza da query de títulos (remove sufixos de autocomplete)
    clean_q = re.sub(r"\s*\((?:prazo|entrega|vence|data):[^\)]*\)", "", raw_query, flags=re.IGNORECASE)
    clean_q = re.sub(r"\s*\[conclu[íi]do\]", "", clean_q, flags=re.IGNORECASE).strip()
    norm_q = normalize_text(clean_q)
    words_q = set(re.findall(r"\w+", norm_q))

    # 5. Busca ponderada com pontuação
    best_item = None
    best_score = 0.0

    for aid, item in assignments.items():
        t_norm = normalize_text(item.get("title", ""))
        c_norm = normalize_text(item.get("course", ""))
        full_norm = f"{t_norm} {c_norm}".strip()
        words_item = set(re.findall(r"\w+", t_norm))

        score = 0.0
        # a. Match exato de título
        if norm_q == t_norm:
            score = 100.0
        # b. Match exato de título + disciplina
        elif norm_q == full_norm:
            score = 95.0
        # c. O título do item é prefixo da query
        elif norm_q.startswith(t_norm) and len(t_norm) >= 3:
            score = 90.0 + (len(t_norm) / max(len(norm_q), 1)) * 4.0
        # d. A query é prefixo do título do item
        elif t_norm.startswith(norm_q) and len(norm_q) >= 3:
            score = 85.0 + (len(norm_q) / max(len(t_norm), 1)) * 4.0
        # e. Substring bidirecional
        elif t_norm in norm_q and len(t_norm) >= 4:
            score = 80.0 + (len(t_norm) / max(len(norm_q), 1)) * 5.0
        elif norm_q in t_norm and len(norm_q) >= 4:
            score = 75.0 + (len(norm_q) / max(len(t_norm), 1)) * 5.0
        # f. Sobreposição de palavras-chave
        elif words_item and words_item.issubset(words_q):
            score = 70.0 + (len(words_item) / max(len(words_q), 1)) * 10.0
        else:
            # Avalia se compartilha números específicos (ex: unidade 7 e aula 3)
            common = words_q.intersection(words_item)
            nums_q = {w for w in words_q if w.isdigit()}
            nums_item = {w for w in words_item if w.isdigit()}
            if nums_q and nums_q == nums_item and len(common) >= 2:
                score = 60.0 + len(common) * 5.0

        if score > best_score:
            best_score = score
            best_item = item

    if best_score >= 60.0:
        return best_item
    return None


def get_available_courses() -> List[str]:
    """Retorna os nomes das disciplinas que possuem pasta em storage/materials/
    OU que aparecem nas atividades catalogadas no state.json."""
    courses_set: set = set()

    # 1. Disciplinas com pasta de material
    mat_dir = settings.STORAGE_MATERIALS_DIR
    if mat_dir.exists():
        for p in mat_dir.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                courses_set.add(p.name)

    # 2. Disciplinas das atividades no state.json
    try:
        state = DaemonState()
        for item in state.data.get("assignments", {}).values():
            course = item.get("course", "").strip()
            if course:
                courses_set.add(course)
    except Exception:
        pass

    return sorted(courses_set)


import os as _os


def _is_relay_mode() -> bool:
    """Retorna True quando o bot está no modo relay (rodando no Render, sem execução local)."""
    return (
        _os.environ.get("BOT_MODE", "desktop").lower() == "relay"
        or bool(_os.environ.get("RENDER"))
        or bool(_os.environ.get("RENDER_SERVICE_ID"))
    )


def _has_ai_key_configured() -> bool:
    """Verifica se há qualquer chave de IA configurada no ambiente ativo (settings ou os.environ)."""
    g_key = (
        _os.environ.get("GEMINI_API_KEY")
        or _os.environ.get("GOOGLE_API_KEY")
        or getattr(settings, "GEMINI_API_KEY", "")
        or ""
    ).strip()
    c_key = (
        _os.environ.get("ANTHROPIC_API_KEY")
        or getattr(settings, "ANTHROPIC_API_KEY", "")
        or ""
    ).strip()
    d_key = (
        _os.environ.get("DEEPSEEK_API_KEY")
        or getattr(settings, "DEEPSEEK_API_KEY", "")
        or ""
    ).strip()
    return bool(
        (g_key and g_key not in ("sua_chave_gemini_api_aqui", "none", ""))
        or (c_key and c_key not in ("sua_chave_anthropic_aqui", "none", ""))
        or (d_key and d_key not in ("sua_chave_deepseek_aqui", "none", ""))
    )


BYOK_RELAY_MESSAGE = (
    "🔒 **Desktop Runner Offline (BYOK)**\n\n"
    "O servidor na nuvem opera em modo seguro sem consumir chaves de terceiros.\n\n"
    "👉 **Para resolver com sua própria chave gratuita:**\n"
    "1. Inicie o assistente no seu computador pelo arquivo `iniciar.bat` (já configurado no `configurar.bat`).\n"
    "2. Assim que o assistente local estiver aberto, repita o comando `/resolver` para processar pelo seu PC!\n\n"
    "💡 *Dica:* Se preferir resolução direta na nuvem 24/7 sem precisar ligar o PC, adicione sua chave de IA (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY` ou `DEEPSEEK_API_KEY`) nas variáveis de ambiente do Render."
)


async def _get_current_assignments() -> Dict[str, Any]:
    """Retorna o dicionário de tarefas ativas, adaptado para execução local ou relay no Render."""
    if _is_relay_mode():
        try:
            from src.notifier.bridge_manager import cloud_bridge
            assignments = await cloud_bridge.get_published_assignments()
            if assignments:
                return assignments
        except Exception:
            pass

    # Tenta leitura local do state.json
    try:
        state = DaemonState()
        local_assignments = state.data.get("assignments", {})
        if local_assignments:
            return local_assignments
    except Exception:
        pass

    # Se local vazio (ex: Render sem flag explícita), tenta bridge como fallback
    try:
        from src.notifier.bridge_manager import cloud_bridge
        return await cloud_bridge.get_published_assignments()
    except Exception:
        return {}


def _get_sync_assignments() -> Dict[str, Any]:
    """Retorna o catálogo de atividades para contextos síncronos."""
    try:
        state = DaemonState()
        local_assignments = state.data.get("assignments", {})
        if local_assignments:
            return local_assignments
    except Exception:
        pass

    try:
        from src.notifier.bridge_manager import cloud_bridge
        return cloud_bridge._published_assignments or {}
    except Exception:
        return {}


async def course_autocomplete(
    interaction: discord.Interaction,
    current: Optional[str] = ""
) -> List[app_commands.Choice[str]]:
    """Autocomplete interativo para seleção de disciplinas no Discord.

    Funciona em dois modos:
    - Desktop (local): lê de storage/materials/ + assignments do state.json
    - Relay (Render): lê cursos publicados pelo desktop via Bridge API,
      com fallback para o catálogo de tarefas sincronizado
    """
    try:
        courses: List[str] = []
        if _is_relay_mode():
            # Modo relay: busca cursos publicados pelo desktop no Render Hub
            from src.notifier.bridge_manager import cloud_bridge
            courses = await cloud_bridge.get_published_courses()
            if not courses:
                # Fallback: tenta extrair das tarefas publicadas via bridge
                pub_assign = await cloud_bridge.get_published_assignments()
                courses = sorted({
                    item.get("course", "").strip()
                    for item in pub_assign.values()
                    if item.get("course")
                })
        else:
            # Modo desktop: leitura local (materials + state.json)
            courses = get_available_courses()

        if not courses:
            # Fallback cruzado
            try:
                from src.notifier.bridge_manager import cloud_bridge
                courses = await cloud_bridge.get_published_courses()
            except Exception:
                pass

        if not courses:
            return [app_commands.Choice(
                name="⚡ Inicie iniciar.bat no seu PC para ver suas disciplinas",
                value="__offline__"
            )]

        norm_curr = normalize_text(current)
        filtered = [c for c in courses if not norm_curr or norm_curr in normalize_text(c)]
        return [
            app_commands.Choice(name=c[:100], value=c)
            for c in filtered[:25]
        ]
    except Exception as err:
        console.print(f"[bold red]Aviso no course_autocomplete: {err}[/bold red]")
        return []


def _is_task_completed(item: Dict[str, Any]) -> bool:
    """Verifica se uma tarefa no catálogo está com status de concluída/submetida."""
    if item.get("is_submitted"):
        return True
    if item.get("status") in ["submitted", "cancelled"]:
        return True
    sub_status = str(item.get("submission_status", "")).lower()
    if any(t in sub_status for t in ["concluído", "concluido", "enviado para avaliação", "feito", "finalizada"]):
        return True
    time_rem = str(item.get("time_remaining", "")).lower()
    if "enviada" in time_rem and "adiantado" in time_rem:
        return True
    return False


def natural_sort_key(text: str) -> List[Any]:
    """Ordenação alfanumérica natural humana (ex: Aula 1, Aula 2 ... Aula 10 em vez de Aula 10 antes de Aula 2)."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", text)]


async def pending_task_autocomplete(
    interaction: discord.Interaction,
    current: Optional[str] = ""
) -> List[app_commands.Choice[str]]:
    """Autocomplete para /resolver: exibe tarefas e questionários pendentes em ordem alfabética natural (A-Z)."""
    try:
        assignments = await _get_current_assignments()
        if not assignments:
            return [app_commands.Choice(
                name="⚡ Inicie iniciar.bat no seu PC para sincronizar as tarefas",
                value="__offline__"
            )]

        norm_curr = normalize_text(current)
        candidates = []
        for aid, item in assignments.items():
            if _is_task_completed(item):
                continue  # Oculta tarefas já concluídas para manter /resolver focado em pendências

            title = str(item.get("title") or aid).strip()
            course_raw = str(item.get("course") or "").strip()
            course_clean = clean_display_course(course_raw)
            due_str = str(item.get("due_date") or "").strip()
            due = f" (Prazo: {due_str})" if due_str else ""

            label = f"{title} - {course_clean}{due}" if course_clean else f"{title}{due}"
            label = label[:100].strip() or f"Tarefa {aid}"

            search_target = f"{normalize_text(title)} {normalize_text(course_raw)} {normalize_text(course_clean)} {normalize_text(aid)}"
            if not norm_curr or norm_curr in search_target:
                candidates.append((label, str(aid)))

        # Ordena estritamente por ordem alfabética natural (A -> Z), começando das primeiras unidades
        candidates.sort(key=lambda x: natural_sort_key(x[0]))

        choices = [
            app_commands.Choice(name=label, value=aid)
            for label, aid in candidates[:25]
        ]
        return choices
    except Exception as err:
        console.print(f"[bold red]Aviso no pending_task_autocomplete: {err}[/bold red]")
        return []


async def completed_task_autocomplete(
    interaction: discord.Interaction,
    current: Optional[str] = ""
) -> List[app_commands.Choice[str]]:
    """Autocomplete para /refazer: exibe exclusivamente tarefas e questionários já concluídos em ordem alfabética natural."""
    try:
        assignments = await _get_current_assignments()
        if not assignments:
            return [app_commands.Choice(
                name="⚡ Inicie iniciar.bat no seu PC para sincronizar as tarefas",
                value="__offline__"
            )]

        norm_curr = normalize_text(current)
        candidates = []
        for aid, item in assignments.items():
            if not _is_task_completed(item):
                continue  # Exibe apenas itens já concluídos/entregues para serem refeitos

            title = str(item.get("title") or aid).strip()
            course_raw = str(item.get("course") or "").strip()
            course_clean = clean_display_course(course_raw)

            label = f"{title} [Concluído] - {course_clean}" if course_clean else f"{title} [Concluído]"
            label = label[:100].strip() or f"Tarefa {aid}"

            search_target = f"{normalize_text(title)} {normalize_text(course_raw)} {normalize_text(course_clean)} {normalize_text(aid)}"
            if not norm_curr or norm_curr in search_target:
                candidates.append((label, str(aid)))

        # Ordena estritamente por ordem alfabética natural (A -> Z)
        candidates.sort(key=lambda x: natural_sort_key(x[0]))

        choices = [
            app_commands.Choice(name=label, value=aid)
            for label, aid in candidates[:25]
        ]
        return choices
    except Exception as err:
        console.print(f"[bold red]Aviso no completed_task_autocomplete: {err}[/bold red]")
        return []


def resolve_course_materials_dir(disciplina: str) -> Path:
    """Encontra o diretório canônico da disciplina em storage/materials/ ou cria um novo padronizado."""
    mat_root = settings.STORAGE_MATERIALS_DIR
    mat_root.mkdir(parents=True, exist_ok=True)

    norm_target = normalize_text(disciplina)
    exact = mat_root / sanitize_filename(disciplina)
    if exact.exists() and exact.is_dir():
        return exact

    # 1. Procura entre as pastas já existentes no disco
    if norm_target:
        for p in mat_root.iterdir():
            if p.is_dir():
                norm_p = normalize_text(p.name)
                if norm_target in norm_p or norm_p in norm_target:
                    return p

    # 2. Procura nas disciplinas conhecidas ativas (Moodle / Bridge)
    if norm_target:
        for c in get_available_courses():
            norm_c = normalize_text(c)
            if norm_target in norm_c or norm_c in norm_target:
                canonical_path = mat_root / sanitize_filename(c)
                canonical_path.mkdir(parents=True, exist_ok=True)
                return canonical_path

    # 3. Fallback: cria pasta para a disciplina
    exact.mkdir(parents=True, exist_ok=True)
    return exact


def get_course_materials_for_task(tarefa_or_course: str) -> List[Path]:
    """Retorna lista de caminhos de materiais disponíveis em storage/materials/ para a tarefa ou disciplina."""
    if not settings.STORAGE_MATERIALS_DIR.exists():
        return []

    assignments = _get_sync_assignments()
    target_course = ""

    matched = find_assignment_by_query(tarefa_or_course, assignments)
    if matched:
        target_course = matched.get("course", "")
    else:
        target_course = tarefa_or_course

    dest_dir = resolve_course_materials_dir(target_course)
    if not dest_dir.exists() or not dest_dir.is_dir():
        return []

    valid_exts = {".pdf", ".csv", ".docx", ".txt", ".zip", ".xlsx", ".pptx"}
    files = [
        f for f in dest_dir.iterdir()
        if f.is_file() and f.suffix.lower() in valid_exts and f.stat().st_size <= 15 * 1024 * 1024
    ]
    files.sort(key=lambda x: natural_sort_key(x.name))
    return files


async def task_material_autocomplete(
    interaction: discord.Interaction,
    current: Optional[str] = ""
) -> List[app_commands.Choice[str]]:
    """Autocomplete para materiais salvos da disciplina vinculada à tarefa em digitação."""
    try:
        selected_task = ""
        already_chosen = set()
        options = interaction.data.get("options", [])
        for opt in options:
            if opt.get("name") in ["tarefa", "disciplina"]:
                selected_task = str(opt.get("value") or "").strip()
            elif opt.get("name") in ["material_1", "material_2", "material_3", "material"]:
                val = str(opt.get("value") or "").strip()
                if val:
                    already_chosen.add(val)

        materials = get_course_materials_for_task(selected_task) if selected_task else []
        if not materials and settings.STORAGE_MATERIALS_DIR.exists():
            for cdir in settings.STORAGE_MATERIALS_DIR.iterdir():
                if cdir.is_dir():
                    for f in cdir.iterdir():
                        if f.is_file() and f.suffix.lower() in [".pdf", ".csv", ".docx", ".txt", ".zip"]:
                            materials.append(f)

        norm_curr = normalize_text(current)
        choices = []
        for mat in materials:
            if mat.name in already_chosen:
                continue
            if not norm_curr or norm_curr in normalize_text(mat.name):
                choices.append(app_commands.Choice(name=mat.name[:100], value=mat.name))
                if len(choices) >= 25:
                    break
        return choices
    except Exception as err:
        console.print(f"[bold red]Aviso no task_material_autocomplete: {err}[/bold red]")
        return []


class MaterialSelectionView(ui.View):
    """View interativa com Dropdown multi-select para o usuário escolher até 3 materiais de apoio salvos."""

    def __init__(
        self,
        tarefa: str,
        instrucoes: Optional[str],
        attached_files: List[Path],
        available_materials: List[Path],
        send_func: Callable[[str], asyncio.Future],
        interaction_or_ctx: Any,
        is_refazer: bool = False,
        modo: str = "resolver"
    ):
        super().__init__(timeout=180)
        matched = find_assignment_by_query(tarefa, _get_sync_assignments())
        self.expected_course = matched.get("course", "") if matched else ""
        self.tarefa = str(matched["id"]) if matched and "id" in matched else tarefa
        self.instrucoes = instrucoes
        self.attached_files = list(attached_files)
        self.available_materials = available_materials
        self.send_func = send_func
        self.context_handle = interaction_or_ctx
        self.is_refazer = is_refazer
        self.modo = modo
        self.selected_files: List[Path] = []

        options = []
        for p in self.available_materials[:25]:
            size_kb = p.stat().st_size // 1024 if p.exists() else 0
            size_str = f"{size_kb} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            options.append(
                discord.SelectOption(
                    label=p.name[:100],
                    value=p.name,
                    description=f"Tamanho: {size_str}"[:100]
                )
            )

        max_pick = min(3, len(options))
        self.select_menu = ui.Select(
            placeholder=f"Selecione de 1 a {max_pick} materiais de apoio salvos...",
            min_values=0,
            max_values=max_pick,
            options=options,
            row=0
        )
        self.select_menu.callback = self.on_select_materials
        self.add_item(self.select_menu)

    async def on_select_materials(self, interaction: discord.Interaction):
        chosen_names = set(self.select_menu.values)
        self.selected_files = [p for p in self.available_materials if p.name in chosen_names]
        chosen_str = ", ".join(f"`{p.name}`" for p in self.selected_files) or "Nenhum material selecionado"
        await interaction.response.send_message(
            f"✅ Selecionado ({len(self.selected_files)}/3): {chosen_str}\nClique em **'🚀 Iniciar Resolução'** para prosseguir.",
            ephemeral=True
        )

    @ui.button(label="🚀 Iniciar Resolução", style=discord.ButtonStyle.green, row=1)
    async def btn_start(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        for item in self.children:
            item.disabled = True
        try:
            if hasattr(self.context_handle, "edit_original_response"):
                await self.context_handle.edit_original_response(view=self)
            elif hasattr(interaction, "message") and interaction.message:
                await interaction.message.edit(view=self)
        except Exception:
            pass

        combined_files = list(self.attached_files)
        for sf in self.selected_files:
            if sf not in combined_files:
                combined_files.append(sf)

        requester = interaction.user.display_name if interaction.user else "Usuário"
        channel = getattr(self.context_handle, "channel", None) or getattr(interaction, "channel", None)
        await enqueue_solve_flow(
            send_func=self.send_func,
            tarefa=self.tarefa,
            instrucoes=self.instrucoes,
            extra_files=combined_files,
            is_refazer=self.is_refazer,
            modo=self.modo,
            requester=requester,
            channel=channel,
            expected_course=self.expected_course
        )

    @ui.button(label="⏩ Resolver sem materiais extras", style=discord.ButtonStyle.secondary, row=1)
    async def btn_skip(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        for item in self.children:
            item.disabled = True
        try:
            if hasattr(self.context_handle, "edit_original_response"):
                await self.context_handle.edit_original_response(view=self)
            elif hasattr(interaction, "message") and interaction.message:
                await interaction.message.edit(view=self)
        except Exception:
            pass

        requester = interaction.user.display_name if interaction.user else "Usuário"
        channel = getattr(self.context_handle, "channel", None) or getattr(interaction, "channel", None)
        await enqueue_solve_flow(
            send_func=self.send_func,
            tarefa=self.tarefa,
            instrucoes=self.instrucoes,
            extra_files=self.attached_files,
            is_refazer=self.is_refazer,
            modo=self.modo,
            requester=requester,
            channel=channel,
            expected_course=self.expected_course
        )


def extract_quiz_answers_payload(
    structured_answers: Optional[Any],
    file_to_submit: Optional[Path] = None
) -> Dict[str, Any]:
    """Extrai mapeamento chave/valor de respostas estruturadas ou analisa o arquivo de rascunho Markdown."""
    ans_payload = {}
    if isinstance(structured_answers, list):
        for item in structured_answers:
            if isinstance(item, dict) and "key" in item:
                ans_payload[item["key"]] = item["value"]
            elif isinstance(item, dict) and "field" in item:
                ans_payload[item["field"]] = item.get("value", "")
    elif isinstance(structured_answers, dict):
        ans_payload = dict(structured_answers)

    # Fallback robusto: se não houver chaves estruturadas, recupera do arquivo de rascunho gerado
    if not ans_payload and file_to_submit:
        target_md = None
        if file_to_submit.suffix == ".md" and file_to_submit.exists():
            target_md = file_to_submit
        elif file_to_submit.parent.exists():
            same_dir_rascunhos = list(file_to_submit.parent.glob(f"{file_to_submit.stem}*_rascunho.md"))
            if same_dir_rascunhos:
                target_md = same_dir_rascunhos[0]
            else:
                any_rascunho = list(file_to_submit.parent.glob("*_rascunho.md"))
                if any_rascunho:
                    target_md = any_rascunho[0]

        if target_md and target_md.exists():
            try:
                text = target_md.read_text(encoding="utf-8")
                # 1. Tenta recuperar bloco JSON estruturado se presente no arquivo
                json_match = re.search(r"```(?:json:answers|json)\s*\n(.*?)\n```", text, re.DOTALL)
                if json_match:
                    try:
                        parsed_json = json.loads(json_match.group(1).strip())
                        if isinstance(parsed_json, dict):
                            ans_payload.update(parsed_json)
                    except Exception:
                        pass

                # 2. Parsing das seções de questões
                q_matches = list(re.finditer(r"###\s*(?:Quest[ãa]o|Q)\s*(\d+)\s*\n+(.*?)(?=\n###|\Z)", text, re.DOTALL | re.IGNORECASE))
                for m in q_matches:
                    q_num = m.group(1)
                    q_body = m.group(2).strip()
                    ans_key = f"Q{q_num}"
                    if ans_key not in ans_payload:
                        resp_m = re.search(r"\*\*(?:Resposta|Alternativa):\*\*\s*([\s\S]+?)(?=\n\*\*(?:Explicação|Justificativa):|\n###|\Z)", q_body, re.IGNORECASE)
                        if resp_m and resp_m.group(1).strip():
                            ans_payload[ans_key] = resp_m.group(1).strip()
                        else:
                            items = re.findall(r"^\s*\d+\.\s*\*{0,2}(.*?)\*{0,2}\s*$", q_body, re.MULTILINE)
                            if items:
                                for idx_sub, sub_val in enumerate(items, 1):
                                    clean_val = sub_val.strip("* ").strip()
                                    if clean_val:
                                        ans_payload[f"Q{q_num}_{idx_sub}"] = clean_val
                                        if any(sep in clean_val for sep in ["→", "->", ":"]):
                                            parts = re.split(r"[→\->:]", clean_val, maxsplit=1)
                                            if len(parts) == 2:
                                                k_label = parts[0].strip("* ").strip()
                                                v_target = parts[1].strip("* ").strip()
                                                if k_label and v_target:
                                                    ans_payload[f"Q{q_num}_{k_label}"] = v_target
                                                    ans_payload[k_label] = v_target
                            else:
                                # Fallback de resposta dissertativa aberta
                                clean_body = re.sub(r"^(?:Texto da questão|Enunciado:?|Pergunta:?)\s*", "", q_body, flags=re.IGNORECASE).strip()
                                if clean_body:
                                    ans_payload[ans_key] = clean_body
            except Exception:
                pass

    return ans_payload


class SessionExpiredView(ui.View):
    """View interativa com botão para abrir a janela de login do Moodle no computador."""

    def __init__(self, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)

    @ui.button(label="Abrir Login no PC", style=discord.ButtonStyle.primary, emoji="🔑", custom_id="btn_open_moodle_login")
    async def open_login_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            from config.settings import settings
            cookies_exist = Path(settings.STORAGE_COOKIES_PATH).exists()
            render_url = (settings.RENDER_URL or "").rstrip("/")

            if not cookies_exist and render_url:
                from src.notifier.bridge_manager import cloud_bridge
                task_id = await cloud_bridge.dispatch_action(
                    action="relogin",
                    channel_id=str(interaction.channel_id),
                    message_id=str(interaction.message.id) if interaction.message else "",
                    requester=interaction.user.name,
                    title="Renovação de Sessão Moodle",
                    course="Autenticação"
                )
                await interaction.followup.send(
                    f"🚀 **Solicitação enviada para o seu computador!** (ID: `{task_id}`)\n"
                    "A janela do navegador para login no MinhaUFMG está sendo aberta no desktop.",
                    ephemeral=True
                )
            else:
                from src.auth.moodle_auth import MoodleAuth
                auth = MoodleAuth()

                async def _bg_login():
                    success = await auth.interactive_login(headless=False)
                    if success:
                        notifier = MoodleDiscordNotifier()
                        valid, user = await auth.validate_session()
                        await notifier.send_session_renewed_notification(user_name=user)

                asyncio.create_task(_bg_login())
                await interaction.followup.send(
                    "🖥️ **Janela de login aberta no seu computador!**\n"
                    "Basta preencher seu usuário e senha do MinhaUFMG na tela do navegador que acabou de abrir. "
                    "Assim que entrar, o assistente salvará a nova sessão e confirmará aqui.",
                    ephemeral=True
                )
        except Exception as err:
            console.print(f"[red]Erro ao disparar login pelo botão do Discord: {err}[/red]")
            await interaction.followup.send(f"❌ Erro ao abrir janela de login: {err}", ephemeral=True)


class ReviewActionView(ui.View):
    """Componente interativo com botões dinâmicos de decisão e persistência imediata em disco."""

    def __init__(
        self,
        assignment_id: str,
        assignment_url: str,
        file_to_submit: Optional[Path] = None,
        activity_type: str = "assign",
        structured_answers: Optional[List[Dict[str, Any]]] = None,
        on_action: Optional[Callable[[str, str, discord.Interaction], asyncio.Future]] = None,
        draft_saved: bool = False,
        is_finalized: bool = False,
        timeout: Optional[float] = None,
        draft: Optional[Any] = None,  # SolutionDraft para suporte a revisão
    ):
        super().__init__(timeout=timeout)
        self.assignment_id = assignment_id
        self.assignment_url = assignment_url
        self.file_to_submit = file_to_submit
        self.activity_type = activity_type
        self.structured_answers = structured_answers or []
        self.on_action = on_action
        self._is_draft_saved = draft_saved
        self._is_finalized = is_finalized
        self._draft = draft  # SolutionDraft associado (para revisão)

        self._build_buttons(draft_saved=draft_saved, is_finalized=is_finalized)

    def _build_buttons(self, draft_saved: bool = False, is_finalized: bool = False):
        self.clear_items()

        if is_finalized:
            btn_done = ui.Button(
                label="Enviado com Sucesso",
                style=discord.ButtonStyle.success,
                emoji="✔",
                disabled=True
            )
            self.add_item(btn_done)
            if self.assignment_url:
                btn_moodle = ui.Button(
                    label="Abrir no Moodle",
                    style=discord.ButtonStyle.link,
                    url=self.assignment_url,
                    emoji="🔗"
                )
                self.add_item(btn_moodle)
            return

        if self.activity_type == "quiz":
            # Row 0: Ações principais do Quiz
            btn_fill = ui.Button(
                label="Apenas Preencher Quiz" if not draft_saved else "✔ Respostas Preenchidas",
                style=discord.ButtonStyle.primary if not draft_saved else discord.ButtonStyle.secondary,
                emoji="📝",
                custom_id=f"btn_fill_{self.assignment_id}",
                disabled=draft_saved,
                row=0
            )
            btn_fill.callback = self.fill_quiz_button
            self.add_item(btn_fill)

            btn_finalize = ui.Button(
                label="Enviar Tudo e Terminar",
                style=discord.ButtonStyle.success,
                emoji="🚀",
                custom_id=f"btn_finalize_{self.assignment_id}",
                row=0
            )
            btn_finalize.callback = self.finalize_quiz_button
            self.add_item(btn_finalize)

            # Botão Modificar para o Quiz (sempre ativo antes da finalização)
            btn_revise = ui.Button(
                label="Modificar",
                style=discord.ButtonStyle.secondary,
                emoji="✏️",
                custom_id=f"btn_revise_{self.assignment_id}",
                row=0
            )
            btn_revise.callback = self.revise_button
            self.add_item(btn_revise)

        else:
            # Row 0: Ações principais da Tarefa
            has_docx = self.file_to_submit and str(self.file_to_submit).endswith(".docx")
            btn_approve = ui.Button(
                label="Aprovar e Enviar" if not draft_saved else "✔ Enviado (Rascunho)",
                style=discord.ButtonStyle.success if not draft_saved else discord.ButtonStyle.secondary,
                emoji="📄",
                custom_id=f"btn_approve_{self.assignment_id}",
                disabled=draft_saved,
                row=0
            )
            btn_approve.callback = self.approve_assign_button
            self.add_item(btn_approve)

            # Botão Modificar para a Tarefa (sempre ativo antes da finalização)
            btn_revise = ui.Button(
                label="Modificar",
                style=discord.ButtonStyle.primary,
                emoji="✏️",
                custom_id=f"btn_revise_{self.assignment_id}",
                row=0
            )
            btn_revise.callback = self.revise_button
            self.add_item(btn_revise)

        # Row 1: Controle de agendamento e descarte
        btn_postpone = ui.Button(
            label="Adiar (+1h)",
            style=discord.ButtonStyle.secondary,
            emoji="⏱️",
            custom_id=f"btn_postpone_{self.assignment_id}",
            row=1
        )
        btn_postpone.callback = self.postpone_button
        self.add_item(btn_postpone)

        btn_cancel = ui.Button(
            label="Cancelar / Descartar",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"btn_cancel_{self.assignment_id}",
            row=1
        )
        btn_cancel.callback = self.cancel_button
        self.add_item(btn_cancel)

        if self.assignment_url:
            btn_moodle = ui.Button(
                label="Abrir no Moodle",
                style=discord.ButtonStyle.link,
                url=self.assignment_url,
                emoji="🔗",
                row=1
            )
            self.add_item(btn_moodle)

    def _extract_answers_payload(self) -> Dict[str, Any]:
        return extract_quiz_answers_payload(self.structured_answers, self.file_to_submit)

    def _recover_draft(self):
        """Recupera ou reconstrói o SolutionDraft caso a view tenha sido recriada sem o draft em memória."""
        try:
            from src.solver.gemini_solver import SolutionDraft
            from src.scheduler.state import DaemonState
            state = DaemonState()
            item = state.get_assignment(self.assignment_id) or {}
            course_name = item.get("course", "Geral")
            title = item.get("title", f"Atividade {self.assignment_id}")

            f_path = self.file_to_submit
            if not f_path or not Path(f_path).exists():
                draft_p = item.get("draft_path")
                if draft_p and Path(draft_p).exists():
                    f_path = Path(draft_p)

            md_content = ""
            docx_path = None
            pdf_path = None
            if f_path and Path(f_path).exists():
                p = Path(f_path)
                if p.suffix.lower() == ".md":
                    md_content = p.read_text(encoding="utf-8", errors="ignore")
                elif p.suffix.lower() == ".docx":
                    docx_path = p
                    adj_md = p.with_suffix(".md")
                    if adj_md.exists():
                        md_content = adj_md.read_text(encoding="utf-8", errors="ignore")
                    else:
                        adj_md2 = p.parent / f"{p.stem}_rascunho.md"
                        if adj_md2.exists():
                            md_content = adj_md2.read_text(encoding="utf-8", errors="ignore")
                elif p.suffix.lower() == ".pdf":
                    pdf_path = p
                    adj_md = p.with_suffix(".md")
                    if adj_md.exists():
                        md_content = adj_md.read_text(encoding="utf-8", errors="ignore")

            if not md_content:
                md_content = f"### Resolução de {title}\nConteúdo da resolução."

            recovered = SolutionDraft(
                assignment_id=self.assignment_id,
                assignment_title=title,
                course_name=course_name,
                summary=md_content[:300],
                full_markdown=md_content,
                output_path=Path(f_path) if f_path else Path("storage/submissions/rascunho.md"),
                docx_path=docx_path,
                pdf_path=pdf_path,
                structured_answers=self.structured_answers,
                activity_type=self.activity_type,
            )
            self._draft = recovered
            return recovered
        except Exception as e:
            console.print(f"[yellow]Aviso ao recuperar draft: {e}[/yellow]")
            return None

    async def revise_button(self, interaction: discord.Interaction):
        """Abre modal para o usuário digitar suas instruções de modificação."""
        draft = self._draft or self._recover_draft()
        if not draft:
            await interaction.response.send_message(
                "⚠️ Rascunho não disponível para revisão. Gere a resolução novamente com `/resolver` ou `/refazer`.",
                ephemeral=True
            )
            return
        modal = RevisionModal(
            draft=draft,
            assignment_id=self.assignment_id,
            assignment_url=self.assignment_url,
            title="✏️ Modificar Resolução",
            activity_type=self.activity_type,
        )
        await interaction.response.send_modal(modal)

    async def fill_quiz_button(self, interaction: discord.Interaction):
        """Apenas preenche os campos do questionário e salva como rascunho (sem finalizar), passando pela fila."""
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.gold()
            embed.set_footer(
                text=f"Status: ⏳ Na fila para preenchimento no Moodle por {interaction.user.name}..."
            )

        await interaction.response.edit_message(embed=embed, view=self)

        state = DaemonState()
        item_data = state.data.get("assignments", {}).get(self.assignment_id, {})
        title_raw = item_data.get("title") or (embed.title if embed else f"Quiz {self.assignment_id}")
        clean_title = title_raw.replace("📋 Revisão: ", "").replace("📋 Revisão de Atividade: ", "").replace("📝 Rascunho Salvo: ", "")
        course_name = clean_display_course(item_data.get("course", "Geral"))

        # Verificação de Ponte Nuvem (Render sem cookies locais)
        if not Path(settings.STORAGE_COOKIES_PATH).exists():
            ans_payload = self._extract_answers_payload()
            task_id = await cloud_bridge.dispatch_action(
                action="fill_quiz",
                assignment_id=self.assignment_id,
                assignment_url=self.assignment_url,
                channel_id=str(interaction.channel_id),
                message_id=str(interaction.message.id),
                requester=interaction.user.name,
                title=clean_title,
                course=course_name,
                structured_answers=ans_payload or self.structured_answers
            )
            if embed:
                embed.color = discord.Color.gold()
                embed.set_footer(
                    text=f"Status: ⏳ Despachado para o Executor Desktop ({task_id})..."
                )
                await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                f"📝 **Preenchimento de Quiz Despachado para o Desktop!** (ID: `{task_id}`)\n"
                f"• Questionário: **{clean_title}**\n"
                f"• O seu executor desktop local irá preencher as respostas no Moodle com sua sessão local da UFMG.",
                ephemeral=False
            )
            return

        async def _do_fill():
            console.print(
                f"[bold cyan]Preenchimento de rascunho executado da fila para {self.assignment_id}![/bold cyan] "
                f"Preenchendo campos no Moodle sem submeter..."
            )
            status_msg = await interaction.followup.send(
                content=f"⏳ **Preenchendo questionário no Moodle com cadência humana ({clean_title})...** As respostas serão digitadas e salvas na tentativa sem submeter.",
                ephemeral=False
            )
            reporter = DiscordLiveReporter(
                status_msg,
                f"⏳ **Preenchendo questionário no Moodle com cadência humana ({clean_title})...** As respostas serão digitadas e salvas na tentativa sem submeter."
            )

            ans_payload = self._extract_answers_payload()
            submitter = MoodleSubmitter()
            current_time = datetime.now().strftime("%H:%M:%S")

            success, message = await submitter.submit_quiz(
                quiz_url=self.assignment_url,
                answers=ans_payload or self.structured_answers,
                auto_submit=False,
                on_log=reporter.log
            )

            if success:
                self._is_draft_saved = True
                self._build_buttons(draft_saved=True)

                if embed:
                    embed.color = discord.Color.blue()
                    embed.title = f"📝 Rascunho Salvo: {clean_title}"
                    embed.set_footer(
                        text=f"Respostas salvas no Moodle às {current_time}. Aguardando sua conferência manual ou envio definitivo."
                    )

                await interaction.message.edit(embed=embed, view=self)
                moodle_link_md = f"👉 **[Clique aqui para abrir sua tentativa no Moodle]({self.assignment_url})**\n\n" if self.assignment_url else ""
                await reporter.finish(
                    f"🎉 **Respostas salvas no Moodle com sucesso!**\n"
                    f"{message}\n\n"
                    f"{moodle_link_md}"
                    f"• Quando terminar de conferir, você mesmo pode clicar em **'Enviar tudo e terminar'** diretamente no Moodle;\n"
                    f"• Ou, se preferir, pode clicar no botão **[🚀 Enviar Tudo e Terminar]** acima para o robô finalizar!"
                )
            else:
                self._build_buttons(draft_saved=False)
                if embed:
                    embed.color = discord.Color.red()
                    embed.set_footer(
                        text=f"Falha ao preencher às {current_time}: {message[:100]}"
                    )
                await interaction.message.edit(embed=embed, view=self)
                await reporter.finish(f"⚠️ **Falha ao preencher questionário no Moodle:** {message}")

            return success, message

        item = QueueItem(
            task_type=QueueTaskType.FILL_QUIZ,
            title=clean_title,
            course=course_name,
            requester=interaction.user.display_name,
            coro_func=_do_fill
        )
        pos = await queue_manager.enqueue(item)
        if pos > 1 or queue_manager.is_busy_except(item):
            queue_mention = f"<#{settings.DISCORD_QUEUE_CHANNEL_ID}>" if settings.DISCORD_QUEUE_CHANNEL_ID else "canal da fila"
            await interaction.followup.send(
                f"📥 **Preenchimento do quiz adicionado à fila!** (Posição: **#{pos}**)\n"
                f"• Atividade: **{clean_title}**\n"
                f"• Acompanhe a ordem e o andamento no {queue_mention}.",
                ephemeral=True
            )

    async def finalize_quiz_button(self, interaction: discord.Interaction):
        """Finaliza e submete em definitivo o questionário no Moodle ('Enviar tudo e terminar'), passando pela fila."""
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.gold()
            embed.set_footer(
                text=f"Status: ⏳ Na fila para finalização no Moodle por {interaction.user.name}..."
            )

        await interaction.response.edit_message(embed=embed, view=self)

        state = DaemonState()
        item_data = state.data.get("assignments", {}).get(self.assignment_id, {})
        title_raw = item_data.get("title") or (embed.title if embed else f"Quiz {self.assignment_id}")
        clean_title = title_raw.replace("📋 Revisão: ", "").replace("📋 Revisão de Atividade: ", "").replace("📝 Rascunho Salvo: ", "")
        course_name = clean_display_course(item_data.get("course", "Geral"))

        # Verificação de Ponte Nuvem (Render sem cookies locais)
        if not Path(settings.STORAGE_COOKIES_PATH).exists():
            ans_payload = self._extract_answers_payload()
            task_id = await cloud_bridge.dispatch_action(
                action="finalize_quiz",
                assignment_id=self.assignment_id,
                assignment_url=self.assignment_url,
                channel_id=str(interaction.channel_id),
                message_id=str(interaction.message.id),
                requester=interaction.user.name,
                title=clean_title,
                course=course_name,
                structured_answers=ans_payload or self.structured_answers
            )
            if embed:
                embed.color = discord.Color.gold()
                embed.set_footer(
                    text=f"Status: ⏳ Despachado para o Executor Desktop ({task_id})..."
                )
                await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                f"🚀 **Envio Definitivo Despachado para o Desktop!** (ID: `{task_id}`)\n"
                f"• Questionário: **{clean_title}**\n"
                f"• O seu executor desktop local irá confirmar 'Enviar tudo e terminar' no Moodle.",
                ephemeral=False
            )
            return

        async def _do_finalize():
            console.print(
                f"[bold cyan]Envio definitivo executado da fila para {self.assignment_id}![/bold cyan]"
            )
            status_msg = await interaction.followup.send(
                content=f"🚀 **Finalizando questionário no Moodle ({clean_title})...** Confirmando 'Enviar tudo e terminar'.",
                ephemeral=False
            )
            reporter = DiscordLiveReporter(
                status_msg,
                f"🚀 **Finalizando questionário no Moodle ({clean_title})...** Confirmando 'Enviar tudo e terminar'."
            )

            submitter = MoodleSubmitter()
            current_time = datetime.now().strftime("%H:%M:%S")

            ans_payload = self._extract_answers_payload()
            if self._is_draft_saved:
                success, message = await submitter.finalize_quiz(self.assignment_url, on_log=reporter.log)
                if not success and "não foi encontrado" in message.lower():
                    # Fallback: tenta preencher e enviar em um passo só
                    success, message = await submitter.submit_quiz(
                        quiz_url=self.assignment_url,
                        answers=ans_payload or self.structured_answers,
                        auto_submit=True,
                        on_log=reporter.log
                    )
            else:
                success, message = await submitter.submit_quiz(
                    quiz_url=self.assignment_url,
                    answers=ans_payload or self.structured_answers,
                    auto_submit=True,
                    on_log=reporter.log
                )

            if success:
                st = DaemonState()
                st.mark_submitted(self.assignment_id)

                if embed:
                    embed.color = discord.Color.green()
                    embed.title = f"✅ Submetido com Sucesso: {clean_title}"
                    embed.set_footer(
                        text=f"Finalizado no Moodle às {current_time} por {interaction.user.name}"
                    )
                await interaction.message.edit(embed=embed, view=self)
                await reporter.finish(f"🎉 **Confirmação de Envio no Moodle:** {message}")
                if self.on_action:
                    await self.on_action(self.assignment_id, "approved", interaction)
            else:
                self._build_buttons(draft_saved=self._is_draft_saved)
                if embed:
                    embed.color = discord.Color.red()
                    embed.set_footer(
                        text=f"Falha na finalização às {current_time}: {message[:100]}"
                    )
                await interaction.message.edit(embed=embed, view=self)
                await reporter.finish(f"⚠️ **Falha ao finalizar questionário:** {message}")

            return success, message

        item = QueueItem(
            task_type=QueueTaskType.FINALIZE_QUIZ,
            title=clean_title,
            course=course_name,
            requester=interaction.user.display_name,
            coro_func=_do_finalize
        )
        pos = await queue_manager.enqueue(item)
        if pos > 1 or queue_manager.is_busy_except(item):
            queue_mention = f"<#{settings.DISCORD_QUEUE_CHANNEL_ID}>" if settings.DISCORD_QUEUE_CHANNEL_ID else "canal da fila"
            await interaction.followup.send(
                f"📥 **Finalização de quiz adicionada à fila!** (Posição: **#{pos}**)\n"
                f"• Atividade: **{clean_title}**\n"
                f"• Acompanhe a ordem e o andamento no {queue_mention}.",
                ephemeral=True
            )

    async def approve_assign_button(self, interaction: discord.Interaction):
        """Aprova e submete tarefas de entrega de arquivo (PDF/Docx), passando pela fila."""
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.gold()
            embed.set_footer(
                text=f"Status: ⏳ Na fila para envio no Moodle por {interaction.user.name}..."
            )

        await interaction.response.edit_message(embed=embed, view=self)

        if not self.file_to_submit:
            await interaction.followup.send("⚠️ Nenhum arquivo foi anexado a este pedido.", ephemeral=True)
            return

        state = DaemonState()
        item_data = state.data.get("assignments", {}).get(self.assignment_id, {})
        title_raw = item_data.get("title") or (embed.title if embed else f"Tarefa {self.assignment_id}")
        clean_title = title_raw.replace("📋 Revisão: ", "").replace("📋 Revisão de Atividade: ", "")
        course_name = clean_display_course(item_data.get("course", "Geral"))

        # Verificação de Ponte Nuvem (Render sem cookies locais)
        if not Path(settings.STORAGE_COOKIES_PATH).exists():
            file_path_str = str(self.file_to_submit) if self.file_to_submit else ""
            task_id = await cloud_bridge.dispatch_action(
                action="approve_assign",
                assignment_id=self.assignment_id,
                assignment_url=self.assignment_url,
                channel_id=str(interaction.channel_id),
                message_id=str(interaction.message.id),
                requester=interaction.user.name,
                title=clean_title,
                course=course_name,
                file_to_submit=file_path_str,
            )
            if embed:
                embed.color = discord.Color.gold()
                embed.set_footer(
                    text=f"Status: ⏳ Despachado para o Executor Desktop ({task_id})..."
                )
                await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                f"🚀 **Aprovação de Envio Registrada na Nuvem!** (ID: `{task_id}`)\n"
                f"• Atividade: **{clean_title}**\n"
                f"• Ação repassada para o seu **Executor Desktop** local com seu login da UFMG.\n"
                f"*(Se o seu PC já estiver ligado, o envio no Moodle ocorrerá em instantes com seus cookies locais)*",
                ephemeral=False
            )
            return

        async def _do_approve():
            console.print(
                f"[bold cyan]Envio de arquivo executado da fila para {self.assignment_id}![/bold cyan] "
                f"Disparando envio de {self.file_to_submit.name}..."
            )
            status_msg = await interaction.followup.send(
                content=f"⏳ **Enviando arquivo no Moodle ({clean_title}):** `{self.file_to_submit.name}`...",
                ephemeral=False
            )
            reporter = DiscordLiveReporter(
                status_msg,
                f"⏳ **Enviando arquivo no Moodle ({clean_title}):** `{self.file_to_submit.name}`..."
            )

            submitter = MoodleSubmitter()
            current_time = datetime.now().strftime("%H:%M:%S")

            success, message = await submitter.submit_assignment(
                assignment_url=self.assignment_url,
                file_path=self.file_to_submit,
                on_log=reporter.log
            )

            if success:
                st = DaemonState()
                st.mark_submitted(self.assignment_id)

                if embed:
                    embed.color = discord.Color.green()
                    embed.title = f"✅ Submetido com Sucesso: {clean_title}"
                    embed.set_footer(
                        text=f"Enviado no Moodle às {current_time} por {interaction.user.name}"
                    )
                await interaction.message.edit(embed=embed, view=self)
                await reporter.finish(f"🎉 **Confirmação de Envio no Moodle:** {message}")
                if self.on_action:
                    await self.on_action(self.assignment_id, "approved", interaction)
            else:
                self._build_buttons(draft_saved=False)
                if embed:
                    embed.color = discord.Color.red()
                    embed.set_footer(
                        text=f"Falha no envio às {current_time}: {message[:100]}"
                    )
                await interaction.message.edit(embed=embed, view=self)
                await reporter.finish(f"⚠️ **Falha no envio:** {message}")

            return success, message

        item = QueueItem(
            task_type=QueueTaskType.SUBMIT_ASSIGNMENT,
            title=clean_title,
            course=course_name,
            requester=interaction.user.display_name,
            coro_func=_do_approve
        )
        pos = await queue_manager.enqueue(item)
        if pos > 1 or queue_manager.is_busy_except(item):
            queue_mention = f"<#{settings.DISCORD_QUEUE_CHANNEL_ID}>" if settings.DISCORD_QUEUE_CHANNEL_ID else "canal da fila"
            await interaction.followup.send(
                f"📥 **Submissão de arquivo adicionada à fila!** (Posição: **#{pos}**)\n"
                f"• Atividade: **{clean_title}**\n"
                f"• Acompanhe a ordem e o andamento no {queue_mention}.",
                ephemeral=True
            )

    async def postpone_button(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True

        state = DaemonState()
        until_str = state.postpone_assignment(self.assignment_id, hours=1)

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text=f"Status: ⏱️ Adiado por {interaction.user.name} até às {until_str}!")

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            content=f"⏱️ **Lembrete Adiado:** Notificações da tarefa `{self.assignment_id}` pausadas até às **{until_str}**.",
            ephemeral=False
        )

        if self.on_action:
            await self.on_action(self.assignment_id, "postponed", interaction)

    async def cancel_button(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True

        state = DaemonState()
        state.cancel_assignment(self.assignment_id)

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.red()
            embed.set_footer(text=f"Status: ❌ Envio cancelado definitivamente por {interaction.user.name}.")

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            content=f"❌ **Cancelado:** A tarefa foi descartada. O robô não enviará e não emitirá mais alertas sobre ela.",
            ephemeral=False
        )

        if self.on_action:
            await self.on_action(self.assignment_id, "cancelled", interaction)


class MoodleBotClient(commands.Bot):
    """Bot do Discord com sincronização instantânea de Slash Commands por servidor e prefixos."""

    def __init__(self):
        # Slash Commands e notificações operam 100% com Intents.default().
        # Evita a exceção PrivilegedIntentsRequired caso os toggles de Privileged Gateway Intents
        # (Message Content / Server Members) não estejam ativados no Discord Developer Portal.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        if _os.environ.get("BRIDGE_RUNNER") == "1" or getattr(self, "_skip_tree_sync", False):
            return
        try:
            self.add_view(SessionExpiredView())
        except Exception:
            pass
        console.print("[cyan]Sincronizando Slash Commands...[/cyan]")
        try:
            # Sync global (propagação pode demorar até 1h no Discord)
            synced = await self.tree.sync()
            console.print(f"[green]✔ {len(synced)} Slash Commands registrados globalmente![/green]")
        except Exception as sync_err:
            console.print(f"[yellow]Nota na sincronização global: {sync_err}[/yellow]")

    async def on_ready(self):
        console.print(f"[bold green]✔ Bot conectado ao Discord como {self.user} (ID: {self.user.id})![/bold green]")
        for guild in self.guilds:
            try:
                # Copia os comandos globais para o escopo do servidor (propagação INSTANTÂNEA)
                # Isso garante que autocomplete funcione imediatamente sem esperar até 1h
                self.tree.copy_global_to(guild=guild)
                synced_guild = await self.tree.sync(guild=guild)
                console.print(f"[green]✔ {len(synced_guild)} comandos sincronizados instantaneamente em '{guild.name}'![/green]")
            except Exception as e:
                console.print(f"[yellow]Aviso ao sincronizar comandos no servidor {guild.name}: {e}[/yellow]")


        # Inicializa o worker da Fila Centralizada e o Painel Dinâmico
        try:
            queue_manager.start_worker(self)
            await queue_manager.update_discord_dashboard()
        except Exception as q_err:
            console.print(f"[yellow]Aviso ao inicializar fila de tarefas no Discord: {q_err}[/yellow]")

        # Restaura automaticamente arquivos de conteúdo do Discord após reinício (especialmente em nuvem efêmera)
        asyncio.create_task(restore_materials_from_discord(self))

    async def on_member_join(self, member: discord.Member):
        """Ao entrar um novo estudante no servidor, provisiona automaticamente suas 5 salas privadas."""
        if member.bot:
            return
        try:
            res = await provision_user_channels(member.guild, member)
            console.print(f"[bold green]✔ Salas privadas provisionadas automaticamente para {member.display_name} ({res.get('category_name')})![/bold green]")
        except Exception as err:
            console.print(f"[yellow]Aviso ao provisionar salas no on_member_join para {member.display_name}: {err}[/yellow]")


# Instância global do Bot
bot = MoodleBotClient()


async def provision_user_channels(guild: discord.Guild, member: discord.Member) -> Dict[str, Any]:
    """Cria ou recupera categoria privada e os 5 canais do Moodle Bot para um membro específico."""
    # Garante que temos o objeto Member completo via REST HTTP (não depende do cache/members intent)
    try:
        member = await guild.fetch_member(member.id)
    except (discord.NotFound, discord.HTTPException):
        pass  # usa o objeto recebido como fallback

    category_name = f"🔒 Moodle • {member.display_name}"[:100]

    # 1. Procura categoria existente para o membro
    category = None
    clean_member_name = normalize_text(member.name)
    clean_display = normalize_text(member.display_name)

    for cat in guild.categories:
        cat_norm = normalize_text(cat.name)
        if "moodle" in cat_norm:
            if clean_member_name in cat_norm or clean_display in cat_norm or str(member.id) in cat_norm:
                category = cat
                break

    # 2. Se não existir, cria a categoria com permissões restritas (apenas aluno e bot)
    if not category:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                embed_links=True,
                attach_files=True
            )
        }
        category = await guild.create_category(name=category_name, overwrites=overwrites)

    # 3. Especificações dos 5 canais essenciais do assistente
    channel_specs = [
        ("alertas-revisoes", "Alertas de prazos, aprovação/adiamento de tarefas e rascunhos"),
        ("conteudos", "Materiais didáticos e uploads do /adicionarconteudo"),
        ("avisos-turma", "Comunicados dos professores capturados do Moodle"),
        ("fila-tarefas", "Acompanhamento em tempo real da fila de execução"),
        ("estudos-simulados", "Tutor tira-dúvidas /perguntar, simulados /quiz e flashcards")
    ]

    channels_map = {}
    new_channels_created = False
    for ch_name, ch_topic in channel_specs:
        ch = discord.utils.get(category.text_channels, name=ch_name)
        if not ch:
            ch = await guild.create_text_channel(
                name=ch_name,
                category=category,
                topic=ch_topic
            )
            new_channels_created = True
        channels_map[ch_name] = ch

    env_mapping = {
        "DISCORD_CHANNEL_ID": channels_map["alertas-revisoes"].id,
        "DISCORD_CONTENT_CHANNEL_ID": channels_map["conteudos"].id,
        "DISCORD_ANNOUNCEMENTS_CHANNEL_ID": channels_map["avisos-turma"].id,
        "DISCORD_QUEUE_CHANNEL_ID": channels_map["fila-tarefas"].id,
        "DISCORD_STUDY_CHANNEL_ID": channels_map["estudos-simulados"].id,
    }

    # 4. Envia mensagem inaugural no canal de alertas se foi recém-criado
    if new_channels_created:
        try:
            embed = discord.Embed(
                title=f"📦 Salas Pessoais Prontas, {member.display_name}!",
                description=(
                    f"Suas 5 salas privadas exclusivas do **Moodle AI Assistant** foram provisionadas com sucesso!\n"
                    f"Apenas você e o bot têm acesso a esta categoria (`{category.name}`).\n\n"
                    "### 🚀 Como Conectar seu Bot Desktop à Sua Máquina:\n"
                    "1. Na sua máquina, dê duplo clique em `configurar.bat` (ou execute `instalar.bat` na primeira vez);\n"
                    "2. Na seção do Discord, clique no botão **🔍 Auto-Detectar Meus Canais**;\n"
                    "3. Ou, se preferir copiar manualmente, cole estas 5 linhas no seu arquivo `.env`:\n\n"
                    f"```env\n"
                    f"DISCORD_CHANNEL_ID={env_mapping['DISCORD_CHANNEL_ID']}\n"
                    f"DISCORD_CONTENT_CHANNEL_ID={env_mapping['DISCORD_CONTENT_CHANNEL_ID']}\n"
                    f"DISCORD_ANNOUNCEMENTS_CHANNEL_ID={env_mapping['DISCORD_ANNOUNCEMENTS_CHANNEL_ID']}\n"
                    f"DISCORD_QUEUE_CHANNEL_ID={env_mapping['DISCORD_QUEUE_CHANNEL_ID']}\n"
                    f"DISCORD_STUDY_CHANNEL_ID={env_mapping['DISCORD_STUDY_CHANNEL_ID']}\n"
                    f"```\n\n"
                    "Dica: Pegue sua chave gratuita do Gemini no Google AI Studio e inicie o login MinhaUFMG com 1 clique!"
                ),
                color=discord.Color.green()
            )
            embed.set_footer(text="Moodle AI Assistant (UFMG) • Multi-User Desktop Edition")
            await channels_map["alertas-revisoes"].send(content=member.mention, embed=embed)
        except Exception:
            pass

    return {
        "guild_id": guild.id,
        "guild_name": guild.name,
        "category_id": category.id,
        "category_name": category.name,
        "user_id": member.id,
        "user_name": member.name,
        "display_name": member.display_name,
        "channels": env_mapping,
        "channel_objects": channels_map
    }


async def restore_materials_from_discord(client: discord.Client) -> int:
    """Restaura materiais enviados no Discord caso tenham sido apagados (ex: reinício no Render ou PC novo)."""
    restored_count = 0
    try:
        from src.scheduler.state import DaemonState
        state = DaemonState()
        from src.notifier.bridge_manager import cloud_bridge

        content_channels = []
        cfg_id = settings.DISCORD_CONTENT_CHANNEL_ID
        if cfg_id and cfg_id != 0:
            ch = client.get_channel(cfg_id)
            if not ch:
                try:
                    ch = await client.fetch_channel(cfg_id)
                except Exception:
                    ch = None
            if ch:
                content_channels.append(ch)

        for guild in client.guilds:
            for ch in guild.text_channels:
                if ch.name == "conteudos" and ch not in content_channels:
                    content_channels.append(ch)

        for channel in content_channels:
            try:
                async for msg in channel.history(limit=100):
                    if not msg.attachments:
                        continue

                    target_course = ""
                    if msg.author == client.user and msg.embeds:
                        embed = msg.embeds[0]
                        if "Conteúdo Adicionado" in (embed.title or ""):
                            for f in embed.fields:
                                if "disciplina" in f.name.lower() or "matéria" in f.name.lower():
                                    target_course = f.value.strip()
                                    break
                    elif msg.content.startswith("!adicionarconteudo"):
                        parts = msg.content.split(maxsplit=1)
                        if len(parts) > 1:
                            target_course = parts[1].strip()

                    if not target_course:
                        continue

                    dest_dir = resolve_course_materials_dir(target_course)
                    dest_dir.mkdir(parents=True, exist_ok=True)

                    for att in msg.attachments:
                        dest_file = dest_dir / sanitize_filename(att.filename)
                        if not dest_file.exists() or dest_file.stat().st_size == 0:
                            try:
                                await att.save(dest_file)
                                restored_count += 1
                                console.print(
                                    f"[bold green]✔ Material restaurado do histórico do Discord:[/bold green] "
                                    f"{dest_file.name} -> {dest_dir.name}"
                                )
                            except Exception as dl_err:
                                console.print(f"[yellow]Aviso ao baixar anexo {att.filename}: {dl_err}[/yellow]")

                        state.register_custom_material(
                            course=dest_dir.name,
                            filename=dest_file.name,
                            attachment_url=att.url,
                            channel_id=channel.id,
                            message_id=msg.id,
                            uploader=str(msg.author),
                            size=att.size
                        )
                        await cloud_bridge.register_material({
                            "course": dest_dir.name,
                            "filename": dest_file.name,
                            "attachment_url": att.url,
                            "channel_id": channel.id,
                            "message_id": msg.id,
                            "uploader": str(msg.author),
                            "size": att.size
                        })
            except Exception as ch_err:
                console.print(f"[yellow]Aviso ao varrer canal de conteúdos {channel.name}: {ch_err}[/yellow]")

        if restored_count > 0:
            console.print(f"[bold green]✔ Restauração concluída: {restored_count} arquivo(s) restaurados com sucesso![/bold green]")
    except Exception as e:
        console.print(f"[yellow]Aviso geral ao restaurar materiais do Discord: {e}[/yellow]")

    return restored_count


def get_user_provisioned_channels(identifier: str) -> Optional[Dict[str, Any]]:
    """Localiza as 5 salas privadas de um membro por ID numérico, username ou display_name em memória."""
    if not bot.is_ready():
        return None

    clean_id = normalize_text(identifier)
    for guild in bot.guilds:
        target_member = None
        if identifier.isdigit():
            target_member = guild.get_member(int(identifier))

        if not target_member:
            for m in guild.members:
                if normalize_text(m.name) == clean_id or normalize_text(m.display_name) == clean_id:
                    target_member = m
                    break

        target_cat = None
        for cat in guild.categories:
            cat_norm = normalize_text(cat.name)
            if "moodle" in cat_norm:
                if target_member:
                    if normalize_text(target_member.name) in cat_norm or normalize_text(target_member.display_name) in cat_norm:
                        target_cat = cat
                        break
                elif clean_id and clean_id in cat_norm:
                    target_cat = cat
                    break

        if target_cat:
            mapping = {}
            for ch in target_cat.text_channels:
                ch_name = ch.name.lower()
                if "alerta" in ch_name or "revis" in ch_name:
                    mapping["DISCORD_CHANNEL_ID"] = str(ch.id)
                elif "conteudo" in ch_name:
                    mapping["DISCORD_CONTENT_CHANNEL_ID"] = str(ch.id)
                elif "aviso" in ch_name:
                    mapping["DISCORD_ANNOUNCEMENTS_CHANNEL_ID"] = str(ch.id)
                elif "fila" in ch_name:
                    mapping["DISCORD_QUEUE_CHANNEL_ID"] = str(ch.id)
                elif "estudo" in ch_name or "simulado" in ch_name:
                    mapping["DISCORD_STUDY_CHANNEL_ID"] = str(ch.id)

            if len(mapping) >= 3:
                return {
                    "guild_id": str(guild.id),
                    "guild_name": guild.name,
                    "category_name": target_cat.name,
                    "channels": mapping
                }
    return None


def build_tarefas_embed(disciplina: Optional[str] = None) -> discord.Embed:
    """Gera o painel visual das atividades e questionários cadastrados no Moodle."""
    assignments = _get_sync_assignments()

    if not assignments:
        desc = "📋 Nenhuma atividade cadastrada no momento."
        if _is_relay_mode():
            desc += "\n\n⚡ **Dica:** O bot está operando em nuvem no Render. Inicie o `iniciar.bat` no seu computador para sincronizar suas tarefas locais."
        return discord.Embed(
            title="📚 Painel de Atividades - Moodle UFMG",
            description=desc,
            color=discord.Color.blue()
        )

    filtered_items = []
    norm_disc = normalize_text(disciplina) if disciplina else None
    for assign_id, item in assignments.items():
        if norm_disc:
            course_name = normalize_text(item.get("course", ""))
            title_name = normalize_text(item.get("title", ""))
            if norm_disc not in course_name and norm_disc not in title_name:
                continue
        filtered_items.append((assign_id, item))

    title_suffix = f" ({disciplina})" if disciplina else ""
    embed = discord.Embed(
        title=f"📚 Painel de Atividades - Moodle UFMG{title_suffix}",
        description=f"Total de itens monitorados: **{len(filtered_items)}**",
        color=discord.Color.blue()
    )

    trabalhos_pendentes = []
    quizzes_pendentes = []
    concluidas = []

    for assign_id, item in filtered_items:
        title = item.get("title", "Atividade")
        course = item.get("course", "Disciplina")
        due = item.get("due_date") or "Sem prazo"
        status = item.get("status", "")
        act_type = item.get("activity_type", "assign")
        is_sub = item.get("is_submitted", False)

        line = f"• **[{title}]({item.get('url', '')})**\n  🏫 {course} | ⏰ {due}"

        if is_sub or status == "submitted":
            concluidas.append(line)
        elif status == "cancelled":
            concluidas.append(f"{line} *(Cancelado)*")
        elif act_type == "quiz":
            quizzes_pendentes.append(line)
        else:
            trabalhos_pendentes.append(line)

    if trabalhos_pendentes:
        embed.add_field(
            name="📌 Trabalhos e Relatórios Pendentes (Envio de Arquivo)",
            value="\n".join(trabalhos_pendentes[:8]),
            inline=False
        )

    if quizzes_pendentes:
        count = len(quizzes_pendentes)
        sample = "\n".join(quizzes_pendentes[:6])
        if count > 6:
            sample += f"\n*... e mais {count - 6} questionários pendentes. Use `!tarefas ingles` para filtrar!*"
        embed.add_field(
            name=f"📝 Questionários & Quizzes Pendentes ({count})",
            value=sample,
            inline=False
        )

    if not trabalhos_pendentes and not quizzes_pendentes:
        embed.add_field(
            name="⏳ Atividades Pendentes",
            value="🎉 Nenhuma atividade pendente! Tudo em dia.",
            inline=False
        )

    if concluidas:
        count = len(concluidas)
        sample = "\n".join(concluidas[:6])
        if count > 6:
            sample += f"\n*... e mais {count - 6} atividades concluídas.*"
        embed.add_field(
            name=f"✅ Atividades Concluídas ({count})",
            value=sample,
            inline=False
        )

    embed.set_footer(text=f"Moodle AI Assistant • UFMG Virtual • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    return embed


async def build_status_embed() -> discord.Embed:
    """Gera o painel de telemetria e diagnóstico do assistente."""
    auth = MoodleAuth()
    is_valid, user = await auth.validate_session()

    courses = get_available_courses()
    mat_dir = settings.STORAGE_MATERIALS_DIR
    total_files = sum(len(list(p.glob("*.*"))) for p in mat_dir.iterdir() if p.is_dir()) if mat_dir.exists() else 0

    assignments = await _get_current_assignments()
    quizzes = [a for a in assignments.values() if a.get("activity_type") == "quiz"]
    assigns = [a for a in assignments.values() if a.get("activity_type") != "quiz"]

    embed = discord.Embed(
        title="🛰️ Telemetria & Status - Moodle AI Assistant",
        color=discord.Color.green() if is_valid else discord.Color.red()
    )

    hb_min = getattr(settings, "SESSION_HEARTBEAT_INTERVAL_MINUTES", 15) or 15
    embed.add_field(
        name="🔐 Sessão Moodle / MinhaUFMG",
        value=(
            f"{'🟢 **Ativa & Headless**' if is_valid else '🔴 **Inativa/Expirada**'}\n"
            f"Usuário: `{user or 'N/A'}`\n"
            f"💓 Keep-Alive Heartbeat: a cada **{hb_min} min**"
        ),
        inline=False
    )

    embed.add_field(
        name="📚 Base de Conhecimento (Materiais)",
        value=f"• Disciplinas ativas: **{len(courses)}**\n• Arquivos catalogados: **{total_files}**",
        inline=True
    )

    embed.add_field(
        name="📋 Atividades Moodle",
        value=f"• Trabalhos de envio: **{len(assigns)}**\n• Quizzes avaliativos: **{len(quizzes)}**",
        inline=True
    )

    # Detecta provedor ativo para exibição no status
    try:
        from src.solver.ai_solver import get_active_provider
        active_provider_str = get_active_provider()
    except Exception:
        active_provider_str = f"Google Gemini ({settings.GEMINI_MODEL})"

    embed.add_field(
        name="🤖 Provedor IA Ativo (BYOK)",
        value=(
            f"✅ **{active_provider_str}**\n"
            f"_Fallbacks Gemini: `{settings.GEMINI_FALLBACK_MODEL_1}` → `{settings.GEMINI_FALLBACK_MODEL_2}`_"
            if "Gemini" in active_provider_str else
            f"✅ **{active_provider_str}**"
        ),
        inline=False
    )


    embed.set_footer(text=f"Daemon a cada {settings.CHECK_INTERVAL_MINUTES} min • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    return embed


def get_materiais_payload(disciplina: str):
    """Localiza materiais em storage/materials/ e empacota para envio no Discord."""
    mat_dir = resolve_course_materials_dir(disciplina)
    if not mat_dir.exists():
        if _is_relay_mode():
            return None, (
                f"⚡ **Aviso de Nuvem (Render):** Os arquivos de `{disciplina}` ficam salvos no seu computador local.\n"
                f"Inicie o `iniciar.bat` no seu PC para consultar e baixar os materiais diretamente pelo Discord!"
            ), []
        return None, f"❌ Disciplina `{disciplina}` não encontrada em `storage/materials/`.", []

    disciplina = mat_dir.name
    files = [p for p in mat_dir.iterdir() if p.is_file() and p.suffix.lower() in [".pdf", ".csv", ".docx", ".zip"]]
    if not files:
        if _is_relay_mode():
            return None, (
                f"⚡ **Aviso de Nuvem (Render):** Os arquivos de `{disciplina}` ficam salvos no seu computador local.\n"
                f"Inicie o `iniciar.bat` no seu PC para consultar e baixar os materiais diretamente pelo Discord!"
            ), []
        return None, f"📚 Nenhum material baixado encontrado para `{disciplina}`.", []

    embed = discord.Embed(
        title=f"📖 Materiais de Estudo: {disciplina}",
        description=f"Total de arquivos disponíveis: **{len(files)}**\nEnviando os principais materiais abaixo:",
        color=discord.Color.green()
    )

    discord_files = []
    file_lines = []
    for f in files[:5]:
        size_kb = f.stat().st_size // 1024
        file_lines.append(f"• `{f.name}` ({size_kb} KB)")
        if f.stat().st_size <= 25 * 1024 * 1024:
            discord_files.append(discord.File(str(f), filename=f.name))

    embed.add_field(name="Arquivos Anexados", value="\n".join(file_lines), inline=False)
    return embed, None, discord_files


# ----------------------------------------------------
# 1. Comandos de Barra (Slash Commands)
# ----------------------------------------------------

@bot.tree.command(name="tarefas", description="Lista as atividades do Moodle (pendentes e já entregues)")
@app_commands.describe(disciplina="Filtrar por disciplina (opcional)")
@app_commands.autocomplete(disciplina=course_autocomplete)
async def cmd_tarefas(interaction: discord.Interaction, disciplina: Optional[str] = None):
    await interaction.response.defer(ephemeral=False)
    embed = build_tarefas_embed(disciplina=disciplina)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="materiais", description="Envia no chat slides e materiais de estudo de uma disciplina")
@app_commands.describe(disciplina="Nome da disciplina cadastrada")
@app_commands.autocomplete(disciplina=course_autocomplete)
async def cmd_materiais(interaction: discord.Interaction, disciplina: str):
    await interaction.response.defer(ephemeral=False)
    embed, err, discord_files = get_materiais_payload(disciplina)
    if err:
        await interaction.followup.send(err)
    else:
        await interaction.followup.send(embed=embed, files=discord_files)


def format_reference_materials_msg(course_name: str, extra_files: List[Path]) -> str:
    """Gera texto informativo claro sobre os arquivos que a IA utilizará como referência."""
    lines = []
    if extra_files:
        attached_user = [p.name for p in extra_files if "temp_uploads" in str(p)]
        selected_mats = [p.name for p in extra_files if "temp_uploads" not in str(p)]

        if attached_user:
            lines.append(f"📎 **Arquivo(s) anexado(s) por você:** {', '.join(f'`{n}`' for n in attached_user)}")
        if selected_mats:
            lines.append(f"📚 **Material(is) de apoio selecionado(s) da matéria:** {', '.join(f'`{n}`' for n in selected_mats)}")
    else:
        lines.append("ℹ️ **Nenhum material de apoio externo selecionado.** A IA resolverá com base estritamente no enunciado e nas questões extraídos diretamente do Moodle.")

    return "\n".join(lines)


async def _execute_solve_flow(
    send_func: Callable[[str], asyncio.Future],
    tarefa: str,
    instrucoes: Optional[str] = None,
    extra_files: Optional[List[Path]] = None,
    is_refazer: bool = False,
    modo: str = "resolver",
    channel: Optional[Any] = None,
    expected_course: Optional[str] = None
):
    assignments = await _get_current_assignments()

    target_item = find_assignment_by_query(tarefa, assignments)

    if not target_item:
        if tarefa.startswith("http") and "/mod/" in tarefa:
            target_item = {
                "id": "custom_" + str(int(datetime.now().timestamp())),
                "title": tarefa,
                "course": expected_course or "Geral / Sob Demanda",
                "url": tarefa,
                "due_date": "Sob demanda",
                "time_remaining": "N/A",
                "activity_type": "quiz" if "mod/quiz" in tarefa else "assign"
            }
        else:
            err_msg = (
                f"❌ **Atividade não encontrada:** `{tarefa}`.\n"
                "Não foi possível localizar essa atividade no catálogo do Moodle UFMG.\n"
                "💡 *Dica:* Digite `/tarefas` para consultar as atividades disponíveis ou use a seleção do `/resolver`."
            )
            await send_func(err_msg)
            return False, f"Atividade '{tarefa}' não encontrada no catálogo do Moodle."

    title = target_item.get("title", tarefa)
    course = target_item.get("course", "Geral")

    # VALIDAÇÃO DE SEGURANÇA CONTRA TROCA DE DISCIPLINAS:
    if expected_course and expected_course != "Geral":
        norm_exp = normalize_text(clean_display_course(expected_course))
        norm_act = normalize_text(clean_display_course(course))
        if norm_exp and norm_act and norm_act != "geral":
            if norm_exp != norm_act and not (norm_exp in norm_act or norm_act in norm_exp):
                err_msg = (
                    f"⚠️ **Incompatibilidade de Disciplina Detectada:** A solicitação indicou a matéria **{expected_course}**, "
                    f"mas a atividade selecionada pertence a **{course}** (`{title}`).\n"
                    f"A execução foi abortada imediatamente por segurança para não resolver/submeter na matéria errada!"
                )
                await send_func(err_msg)
                return False, f"Abortado por incompatibilidade de disciplinas ({expected_course} vs {course})"

    if _is_relay_mode() and not _has_ai_key_configured():
        from src.notifier.bridge_manager import cloud_bridge
        if await cloud_bridge.is_desktop_online():
            effective_ch_id = str(
                getattr(channel, "id", None)
                or getattr(channel, "channel_id", None)
                or settings.DISCORD_CHANNEL_ID
                or 0
            )
            task_id = await cloud_bridge.dispatch_action(
                action="redo_task" if is_refazer else "solve_task",
                assignment_id=str(target_item.get("id", tarefa)),
                assignment_url=str(target_item.get("url", "")),
                channel_id=effective_ch_id,
                message_id="",
                requester="Discord (Relay)",
                title=title,
                course=course,
                file_to_submit="",
                structured_answers={
                    "instrucoes": instrucoes,
                    "extra_files": [str(p) for p in (extra_files or [])],
                    "is_refazer": is_refazer,
                    "modo": modo,
                    "tarefa": str(target_item.get("id", tarefa)),
                    "expected_course": course,
                    "task_title": title
                }
            )
            await send_func(
                f"🚀 **Encaminhado para o seu Desktop Runner!** (ID: `{task_id}`)\n"
                f"A atividade **{title}** ({course}) está sendo processada no seu computador local com o seu provedor de IA configurado.\n"
                f"Acompanhe o andamento e aguarde o rascunho ser enviado aqui em instantes!"
            )
            res = await cloud_bridge.wait_for_task(task_id, timeout=360.0)
            if res:
                return res.get("success", False), res.get("result_message", "")
            return False, "Tempo limite de resposta do Desktop Runner esgotado (6 min)."
        else:
            await send_func(BYOK_RELAY_MESSAGE)
            return False, "Desktop Runner offline e nenhuma chave de IA configurada na nuvem."

    solver = AISolver()
    # AISolver usa o provedor ativo (Gemini/Claude/DeepSeek) — BYOK
    # Não é necessário checar client aqui: AISolver levanta RuntimeError se sem chave

    assign_obj = Assignment(
        id=target_item.get("id", "1"),
        course_id="",
        course_name=target_item.get("course", "Geral"),
        title=target_item.get("title", tarefa),
        url=target_item.get("url", ""),
        description=target_item.get("description", ""),
        activity_type=target_item.get("activity_type", "quiz" if "mod/quiz" in target_item.get("url", "") else "assign")
    )

    reporter = None
    try:
        ref_msg = format_reference_materials_msg(assign_obj.course_name, extra_files or [])
        if modo == "finalizar":
            action_verb = "⚡ Resolvendo e Enviando"
        elif modo == "preencher":
            action_verb = "📝 Resolvendo e Preenchendo"
        elif is_refazer:
            action_verb = "🔄 Refazendo"
        else:
            action_verb = "🧠 Analisando"

        initial_header = f"{action_verb} **{assign_obj.title}**...\n{ref_msg}"
        status_msg = await send_func(initial_header)
        if not status_msg and channel and hasattr(channel, "send"):
            try:
                status_msg = await channel.send(initial_header)
            except Exception:
                pass
        if not status_msg:
            try:
                fallback_ch = await MoodleDiscordNotifier()._resolve_channel()
                if fallback_ch and hasattr(fallback_ch, "send"):
                    status_msg = await fallback_ch.send(initial_header)
            except Exception:
                pass
        reporter = DiscordLiveReporter(status_msg, initial_header)

        has_local_session = Path(settings.STORAGE_COOKIES_PATH).exists() and not _is_relay_mode()

        if assign_obj.activity_type == "quiz":
            ext_res = {"success": False}
            if has_local_session:
                try:
                    from src.scraper.moodle_quiz import MoodleQuizAutomator
                    quiz_automator = MoodleQuizAutomator()
                    ext_res = await quiz_automator.inspect_and_extract_quiz(assign_obj.url, on_log=reporter.log)
                except Exception as q_err:
                    ext_res = {"success": False, "error": str(q_err)}
            else:
                if reporter:
                    await reporter.log("⚡ [Nuvem / Sem Sessão Local] Gerando resolução diretamente com IA...")

            if ext_res.get("success") and ext_res.get("questions"):
                draft = await solver.solve_quiz_with_live_context(
                    assignment=assign_obj,
                    questions_data=ext_res["questions"],
                    user_notes=instrucoes,
                    extra_context_files=extra_files,
                    on_log=reporter.log,
                    auto_triggered=False,  # manual → DOCX editável
                )
            else:
                draft = await solver.solve_assignment(
                    assignment=assign_obj,
                    user_notes=instrucoes,
                    extra_context_files=extra_files,
                    on_log=reporter.log,
                    auto_triggered=False,  # manual → DOCX editável
                )
        else:
            draft = await solver.solve_assignment(
                assignment=assign_obj,
                user_notes=instrucoes,
                extra_context_files=extra_files,
                on_log=reporter.log,
                auto_triggered=False,  # manual → DOCX editável
            )

        submit_success = False
        submit_msg = ""
        notifier = MoodleDiscordNotifier()

        if modo == "preencher":
            await reporter.log("📝 Preenchendo respostas no Moodle sem submeter...")
            submitter = MoodleSubmitter()
            if assign_obj.activity_type == "quiz":
                ans_payload = extract_quiz_answers_payload(draft.structured_answers, draft.output_path)
                submit_success, submit_msg = await submitter.submit_quiz(
                    quiz_url=assign_obj.url,
                    answers=ans_payload or draft.structured_answers,
                    auto_submit=False,
                    on_log=reporter.log
                )
            else:
                # Para DOCX (manual) ou fallback para markdown
                file_to_submit = (
                    draft.docx_path if (draft.docx_path and draft.docx_path.exists())
                    else draft.pdf_path if (draft.pdf_path and draft.pdf_path.exists())
                    else draft.output_path
                )
                submit_success, submit_msg = await submitter.submit_assignment(
                    assignment_url=assign_obj.url,
                    file_path=file_to_submit,
                    on_log=reporter.log
                )
            sent = await notifier.send_assignment_review(
                assign_obj, draft, draft_saved=submit_success, is_finalized=False, final_status_message=submit_msg, channel=channel
            )
            if submit_success:
                await reporter.finish(f"🎉 **Respostas salvas no Moodle com sucesso!** ({assign_obj.title})\n{submit_msg}")
                return True, f"Respostas salvas no Moodle ({assign_obj.title})"
            else:
                await reporter.finish(f"⚠️ Resolução gerada, mas falhou ao preencher no Moodle: {submit_msg}")
                return False, f"Falha ao preencher no Moodle: {submit_msg}"

        elif modo == "finalizar":
            await reporter.log("🚀 Preenchendo e submetendo em definitivo no Moodle...")
            submitter = MoodleSubmitter()
            if assign_obj.activity_type == "quiz":
                ans_payload = extract_quiz_answers_payload(draft.structured_answers, draft.output_path)
                submit_success, submit_msg = await submitter.submit_quiz(
                    quiz_url=assign_obj.url,
                    answers=ans_payload or draft.structured_answers,
                    auto_submit=True,
                    on_log=reporter.log
                )
            else:
                # Para DOCX (manual) ou fallback para markdown
                file_to_submit = (
                    draft.docx_path if (draft.docx_path and draft.docx_path.exists())
                    else draft.pdf_path if (draft.pdf_path and draft.pdf_path.exists())
                    else draft.output_path
                )
                submit_success, submit_msg = await submitter.submit_assignment(
                    assignment_url=assign_obj.url,
                    file_path=file_to_submit,
                    on_log=reporter.log
                )
            if submit_success:
                DaemonState().mark_submitted(assign_obj.id)
            sent = await notifier.send_assignment_review(
                assign_obj, draft, draft_saved=True, is_finalized=submit_success, final_status_message=submit_msg, channel=channel
            )
            if submit_success:
                await reporter.finish(f"🎉 **Atividade finalizada e enviada no Moodle com sucesso!** ({assign_obj.title})\n{submit_msg}")
                return True, f"Atividade finalizada e enviada ({assign_obj.title})"
            else:
                await reporter.finish(f"⚠️ Resolução gerada, mas falhou ao finalizar no Moodle: {submit_msg}")
                return False, f"Falha ao finalizar no Moodle: {submit_msg}"

        else:  # modo == "resolver" (manual → envia DOCX editável)
            sent = await notifier.send_assignment_review(assign_obj, draft, channel=channel)
            if sent:
                file_label = "DOCX editável" if (draft.docx_path and draft.docx_path.exists()) else "rascunho"
                await reporter.finish(
                    f"✔ Resolução de **{assign_obj.title}** enviada como {file_label} com botões de revisão!"
                )
                return True, f"Resolução gerada e enviada como {file_label}"
            else:
                await reporter.finish(f"⚠️ Resolução de **{assign_obj.title}** gerada, mas houve falha ao enviar o card no Discord.")
                return False, "Falha ao enviar o card de revisão no Discord"

    except Exception as e:
        err_msg = str(e)
        try:
            DaemonState().mark_failed(assign_obj.id, err_msg)
        except Exception:
            pass
        if reporter:
            await reporter.finish(f"❌ Erro ao gerar resolução: {err_msg}")
        else:
            await send_func(f"❌ Erro ao gerar resolução: {err_msg}")
        return False, err_msg


async def enqueue_solve_flow(
    send_func: Callable[..., asyncio.Future],
    tarefa: str,
    instrucoes: Optional[str] = None,
    extra_files: Optional[List[Path]] = None,
    is_refazer: bool = False,
    modo: str = "resolver",
    requester: str = "Usuário",
    channel: Optional[Any] = None,
    silent_enqueue: bool = False,
    on_finish: Optional[Callable[[bool, str], Coroutine[Any, Any, Any]]] = None,
    expected_course: Optional[str] = None
) -> int:
    """Enfileira a resolução de uma tarefa ou questionário no TaskQueueManager."""
    assignments = await _get_current_assignments()

    target_item = find_assignment_by_query(tarefa, assignments)

    if not target_item:
        if tarefa.startswith("http") and "/mod/" in tarefa:
            target_item = {
                "id": "custom_" + str(int(datetime.now().timestamp())),
                "title": tarefa,
                "course": expected_course or "Geral / Sob Demanda",
                "url": tarefa,
                "due_date": "Sob demanda",
                "time_remaining": "N/A",
                "activity_type": "quiz" if "mod/quiz" in tarefa else "assign"
            }
        else:
            await send_func(
                f"❌ **Atividade não encontrada:** `{tarefa}`.\n"
                "Não foi possível localizar essa atividade no catálogo do Moodle UFMG.\n"
                "💡 *Dica:* Digite `/tarefas` para ver a lista de atividades pendentes ou use a seleção do `/resolver`."
            )
            return -1

    title = target_item.get("title", tarefa)
    course = clean_display_course(target_item.get("course", "Geral"))
    is_quiz = "mod/quiz" in target_item.get("url", "").lower() or target_item.get("activity_type") == "quiz"

    if is_refazer:
        task_type = QueueTaskType.REDO_TASK
    elif modo == "finalizar":
        task_type = QueueTaskType.PIPELINE_COMPLETE
    elif modo == "preencher":
        task_type = QueueTaskType.PIPELINE_FILL
    elif is_quiz:
        task_type = QueueTaskType.RESOLVE_QUIZ
    else:
        task_type = QueueTaskType.RESOLVE_ASSIGNMENT

    # Criamos função de envio segura com fallback para channel.send
    # caso o token da interação do Discord expire enquanto aguardava na fila
    async def _safe_send(*args, **kwargs):
        try:
            return await send_func(*args, **kwargs)
        except Exception:
            if channel and hasattr(channel, "send"):
                return await channel.send(*args, **kwargs)
            raise

    async def _do_solve():
        return await _execute_solve_flow(
            send_func=_safe_send,
            tarefa=tarefa,
            instrucoes=instrucoes,
            extra_files=extra_files,
            is_refazer=is_refazer,
            modo=modo,
            channel=channel,
            expected_course=expected_course or course
        )

    item = QueueItem(
        task_type=task_type,
        title=title,
        course=course,
        requester=requester,
        coro_func=_do_solve,
        on_finish=on_finish
    )

    pos = await queue_manager.enqueue(item)
    if (pos > 1 or queue_manager.is_busy_except(item)) and not silent_enqueue:
        queue_mention = f"<#{settings.DISCORD_QUEUE_CHANNEL_ID}>" if settings.DISCORD_QUEUE_CHANNEL_ID else "canal da fila"
        if is_refazer:
            verb = "Refazer atividade"
        elif modo == "finalizar":
            verb = "⚡ Resolução & Envio Completo"
        elif modo == "preencher":
            verb = "📝 Resolução & Preenchimento"
        else:
            verb = "🧠 Resolução com IA"

        await _safe_send(
            f"📥 **{verb} adicionada à fila de execução!** (Posição: **#{pos}**)\n"
            f"• Atividade: **{title}**\n"
            f"• Disciplina: **{course}**\n"
            f"• Acompanhe a ordem e o andamento no {queue_mention}."
        )

    return pos


@bot.tree.command(name="resolver", description="Resolve uma tarefa ou questionário pendente com IA")
@app_commands.describe(
    tarefa="ID, link ou nome da tarefa pendente a ser resolvida",
    modo="Modo de execução: apenas resolver, preencher rascunho no Moodle ou enviar tudo",
    instrucoes="Instruções adicionais personalizadas (ex: use linguagem R ou deduza passo a passo)",
    arquivo="Arquivo de referência complementar anexado por você (enunciado, foto ou PDF)",
    material_1="Material 1 salvo da matéria para usar como apoio (opcional)",
    material_2="Material 2 salvo da matéria para usar como apoio (opcional)",
    material_3="Material 3 salvo da matéria para usar como apoio (opcional)"
)
@app_commands.choices(
    modo=[
        app_commands.Choice(name="🧠 Apenas Resolver (Gera rascunho e envia para revisão)", value="resolver"),
        app_commands.Choice(name="📝 Resolver e Preencher (Preenche no Moodle sem submeter)", value="preencher"),
        app_commands.Choice(name="⚡ Resolver, Preencher e Enviar Tudo (End-to-End)", value="finalizar"),
    ]
)
@app_commands.autocomplete(
    tarefa=pending_task_autocomplete,
    material_1=task_material_autocomplete,
    material_2=task_material_autocomplete,
    material_3=task_material_autocomplete
)
async def cmd_resolver(
    interaction: discord.Interaction,
    tarefa: str,
    modo: Optional[app_commands.Choice[str]] = None,
    instrucoes: Optional[str] = None,
    arquivo: Optional[discord.Attachment] = None,
    material_1: Optional[str] = None,
    material_2: Optional[str] = None,
    material_3: Optional[str] = None
):
    await interaction.response.defer(ephemeral=False)
    chosen_modo = modo.value if isinstance(modo, app_commands.Choice) else (modo or "resolver")

    assignments = _get_sync_assignments()
    matched = find_assignment_by_query(tarefa, assignments)
    target_tarefa = str(matched["id"]) if matched and "id" in matched else tarefa
    expected_course = matched.get("course", "") if matched else None

    extra_files: List[Path] = []
    if arquivo:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest_file = temp_dir / sanitize_filename(arquivo.filename)
        await arquivo.save(dest_file)
        extra_files.append(dest_file)

    available_mats = get_course_materials_for_task(target_tarefa)

    # Verifica se o usuário especificou materiais diretamente nos parâmetros do comando
    specified_mats = [m for m in [material_1, material_2, material_3] if m]
    if specified_mats:
        for m_name in specified_mats:
            for p in available_mats:
                if p.name == m_name or normalize_text(p.name) == normalize_text(m_name):
                    if p not in extra_files:
                        extra_files.append(p)
                    break
        await enqueue_solve_flow(
            send_func=interaction.followup.send,
            tarefa=target_tarefa,
            instrucoes=instrucoes,
            extra_files=extra_files,
            is_refazer=False,
            modo=chosen_modo,
            requester=interaction.user.display_name,
            channel=interaction.channel,
            expected_course=expected_course
        )
        return

    # Se não especificou materiais nos parâmetros e há materiais salvos da matéria:
    if available_mats:
        embed = discord.Embed(
            title="📚 Seleção de Materiais de Apoio",
            description=(
                f"Foram identificados **{len(available_mats)} material(is)** salvos para esta disciplina.\n\n"
                "👉 **Selecione no menu abaixo até 3 arquivos** que a IA deve utilizar como referência:\n"
                "*(Ou clique diretamente em 'Resolver sem materiais extras')*"
            ),
            color=discord.Color.blue()
        )
        if extra_files:
            embed.add_field(
                name="📎 Arquivo Anexado por Você",
                value=f"`{extra_files[0].name}` (será enviado obrigatoriamente)",
                inline=False
            )
        view = MaterialSelectionView(
            tarefa=target_tarefa,
            instrucoes=instrucoes,
            attached_files=extra_files,
            available_materials=available_mats,
            send_func=interaction.followup.send,
            interaction_or_ctx=interaction,
            is_refazer=False,
            modo=chosen_modo
        )
        await interaction.followup.send(embed=embed, view=view)
        return

    # Caso não haja materiais salvos para a disciplina:
    await enqueue_solve_flow(
        send_func=interaction.followup.send,
        tarefa=target_tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=False,
        modo=chosen_modo,
        requester=interaction.user.display_name,
        channel=interaction.channel,
        expected_course=expected_course
    )


@bot.tree.command(name="refazer", description="Refaz uma tarefa ou questionário já concluído com IA")
@app_commands.describe(
    tarefa="ID, link ou nome da tarefa concluída a ser refeita",
    instrucoes="Novas instruções ou ajustes desejados (ex: refazer questão 2 com mais detalhes)",
    arquivo="Arquivo de referência complementar anexado por você (enunciado, foto ou PDF)",
    material_1="Material 1 salvo da matéria para usar como apoio (opcional)",
    material_2="Material 2 salvo da matéria para usar como apoio (opcional)",
    material_3="Material 3 salvo da matéria para usar como apoio (opcional)"
)
@app_commands.autocomplete(
    tarefa=completed_task_autocomplete,
    material_1=task_material_autocomplete,
    material_2=task_material_autocomplete,
    material_3=task_material_autocomplete
)
async def cmd_refazer(
    interaction: discord.Interaction,
    tarefa: str,
    instrucoes: Optional[str] = None,
    arquivo: Optional[discord.Attachment] = None,
    material_1: Optional[str] = None,
    material_2: Optional[str] = None,
    material_3: Optional[str] = None
):
    await interaction.response.defer(ephemeral=False)

    assignments = _get_sync_assignments()
    matched = find_assignment_by_query(tarefa, assignments)
    target_tarefa = str(matched["id"]) if matched and "id" in matched else tarefa
    expected_course = matched.get("course", "") if matched else None

    extra_files: List[Path] = []
    if arquivo:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest_file = temp_dir / sanitize_filename(arquivo.filename)
        await arquivo.save(dest_file)
        extra_files.append(dest_file)

    available_mats = get_course_materials_for_task(target_tarefa)

    specified_mats = [m for m in [material_1, material_2, material_3] if m]
    if specified_mats:
        for m_name in specified_mats:
            for p in available_mats:
                if p.name == m_name or normalize_text(p.name) == normalize_text(m_name):
                    if p not in extra_files:
                        extra_files.append(p)
                    break
        await enqueue_solve_flow(
            send_func=interaction.followup.send,
            tarefa=target_tarefa,
            instrucoes=instrucoes,
            extra_files=extra_files,
            is_refazer=True,
            requester=interaction.user.display_name,
            channel=interaction.channel,
            expected_course=expected_course
        )
        return

    if available_mats:
        embed = discord.Embed(
            title="📚 Seleção de Materiais de Apoio (Refazer)",
            description=(
                f"Foram identificados **{len(available_mats)} material(is)** salvos para esta disciplina.\n\n"
                "👉 **Selecione no menu abaixo até 3 arquivos** que a IA deve utilizar como referência:\n"
                "*(Ou clique diretamente em 'Refazer sem materiais extras')*"
            ),
            color=discord.Color.blue()
        )
        if extra_files:
            embed.add_field(
                name="📎 Arquivo Anexado por Você",
                value=f"`{extra_files[0].name}` (será enviado obrigatoriamente)",
                inline=False
            )
        view = MaterialSelectionView(
            tarefa=target_tarefa,
            instrucoes=instrucoes,
            attached_files=extra_files,
            available_materials=available_mats,
            send_func=interaction.followup.send,
            interaction_or_ctx=interaction,
            is_refazer=True
        )
        await interaction.followup.send(embed=embed, view=view)
        return

    await enqueue_solve_flow(
        send_func=interaction.followup.send,
        tarefa=target_tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=True,
        requester=interaction.user.display_name,
        channel=interaction.channel,
        expected_course=expected_course
    )



class RevisionModal(ui.Modal):
    """Modal para o usuário digitar instruções de modificação da resolução gerada pela IA."""

    def __init__(
        self,
        draft,  # SolutionDraft
        assignment_id: str,
        assignment_url: str,
        title: str = "✏️ Modificar Resolução",
        activity_type: str = "assign",
    ):
        super().__init__(title=title[:45])
        self._draft = draft
        self._assignment_id = assignment_id
        self._assignment_url = assignment_url
        self._activity_type = activity_type

        self.revision_input = ui.TextInput(
            label="O que deseja alterar no arquivo/resolução?",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Ex: Mude a questão 3 para alternativa B;\n"
                "Desenvolva o cálculo da Questão 2 com mais detalhes;\n"
                "Corrija o resultado da questão 5..."
            ),
            required=True,
            min_length=3,
            max_length=2000,
        )
        self.add_item(self.revision_input)

    async def on_submit(self, interaction: discord.Interaction):
        instructions = self.revision_input.value.strip()
        await interaction.response.defer(thinking=True)

        try:
            from src.solver.ai_solver import AISolver
            solver = AISolver()

            active_prov = getattr(solver, "active_provider", "deepseek")
            prov_label = "DeepSeek Flash" if active_prov == "deepseek" else ("Claude" if active_prov == "anthropic" else "Gemini")

            status_msg = await interaction.followup.send(
                f"🧠 **Fazendo nova chamada na API ({prov_label}) para aplicar modificações...**\n"
                f"• Atividade: **{self._draft.assignment_title}**\n"
                f"• Modificações solicitadas: *\"{instructions[:180]}{'...' if len(instructions) > 180 else ''}\"*\n"
                f"• Aplicando edições no arquivo gerado...",
                ephemeral=False
            )

            reporter = DiscordLiveReporter(status_msg, f"🧠 Aplicando modificações em **{self._draft.assignment_title}**...")
            await reporter.log(f"Reescrevendo resolução via {prov_label} com raciocínio analítico...")

            new_draft = await solver.apply_revision(
                draft=self._draft,
                revision_instructions=instructions,
                on_log=reporter.log,
            )

            await reporter.log("✔ Modificações aplicadas! Gerando documento atualizado...")

            notifier = MoodleDiscordNotifier()
            act_type = getattr(self._draft, "activity_type", self._activity_type) or "assign"
            assign_obj = Assignment(
                id=self._draft.assignment_id,
                course_id="",
                course_name=self._draft.course_name,
                title=self._draft.assignment_title,
                url=self._assignment_url,
                description="",
                activity_type=act_type,
            )

            target_channel = interaction.channel
            sent = await notifier.send_assignment_review(
                assignment=assign_obj,
                draft=new_draft,
                channel=target_channel,
                final_status_message=f"Modificações aplicadas via {prov_label}: {instructions[:100]}",
            )

            file_name = (new_draft.docx_path or new_draft.output_path or Path("resolucao.docx")).name
            await reporter.finish(
                f"🎉 **Resolução modificada e atualizada com sucesso!**\n"
                f"• Novo arquivo gerado: **`{file_name}`**\n"
                f"• Confira o novo card com os botões de revisão (você pode clicar em **Modificar** novamente se precisar de mais ajustes!)."
            )

        except Exception as e:
            console.print(f"[red]Erro ao aplicar modificações na IA: {e}[/red]")
            await interaction.followup.send(
                f"❌ Erro ao aplicar modificações com a IA: {e}",
                ephemeral=True,
            )


class BatchInstructionModal(ui.Modal, title="Instruções para o Lote"):

    def __init__(self, parent_view: "BatchSelectView"):
        super().__init__()
        self.parent_view = parent_view
        self.instrucoes_input = ui.TextInput(
            label="Instruções personalizadas para a IA",
            style=discord.TextStyle.paragraph,
            placeholder="Ex: Use o gabarito das aulas anteriores, deduza passo a passo, etc.",
            default=parent_view.instrucoes or "",
            required=False,
            max_length=1000
        )
        self.add_item(self.instrucoes_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_val = self.instrucoes_input.value.strip()
        self.parent_view.instrucoes = new_val if new_val else None
        embed = self.parent_view.build_panel_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


def _format_friendly_error(raw_err: Any) -> str:
    """Formata mensagens técnicas de erro da IA/Playwright em explicações legíveis e limpas."""
    err_str = str(raw_err or "Erro desconhecido").strip()
    err_lower = err_str.lower()
    if "503" in err_lower or "unavailable" in err_lower or "high demand" in err_lower:
        return "Servidores com alta demanda (503 / Sobrecarga temporária da IA)"
    if "429" in err_lower or "resource_exhausted" in err_lower or "quota" in err_lower:
        return "Cota de requisições temporariamente excedida (429)"
    if "timeout" in err_lower or "tempo limite" in err_lower:
        return "Tempo limite de resposta esgotado"
    if "não foi possível abrir ou retomar a tentativa" in err_lower:
        return "Tentativa do questionário fechada ou inacessível no Moodle"
    first_line = err_str.split("\n")[0].strip()
    return first_line[:95]


async def _coordinate_batch_followup(
    records: Dict[str, Dict[str, Any]],
    done_events: Dict[str, asyncio.Event],
    chosen_items: List[Dict[str, Any]],
    batch_send: Callable[..., asyncio.Future],
    modo: str,
    instrucoes: Optional[str],
    combined_files: List[Path],
    requester: str,
    channel: Optional[Any],
    user_mention: str
):
    """Monitora a execução sequencial do lote, dispara repescagem automática em caso de falha e envia relatório consolidado."""
    # 1. Aguarda conclusão de todos os itens da 1ª rodada
    for aid, ev in done_events.items():
        try:
            await ev.wait()
        except Exception:
            pass

    # 2. Identifica falhas da 1ª rodada (ex: sobrecarga 503)
    failed_aids = [aid for aid, rec in records.items() if not rec["success"]]

    # 3. Fase 2: Repescagem Automática (Auto-Retry)
    if failed_aids:
        try:
            retry_embed = discord.Embed(
                title="🔄 Repescagem Automática do Lote Iniciada",
                description=(
                    f"Identificamos que **{len(failed_aids)} de {len(chosen_items)} atividade(s)** falharam na primeira tentativa (ex: sobrecarga 503 / instabilidade de conexão).\n\n"
                    f"⏳ *Aguardando 10 segundos para resfriamento da API antes de retentar automaticamente apenas as atividades com falha...*"
                ),
                color=discord.Color.gold()
            )
            await batch_send(embed=retry_embed)
        except Exception:
            pass

        await asyncio.sleep(10.0)

        retry_events = {}
        for aid in failed_aids:
            records[aid]["retried"] = True
            retry_events[aid] = asyncio.Event()

            def _make_retry_finish(target_aid=aid):
                async def _on_retry_finish(success: bool, msg: str):
                    records[target_aid]["success"] = success
                    records[target_aid]["message"] = msg
                    if not success:
                        records[target_aid]["error"] = msg
                    else:
                        records[target_aid]["error"] = None
                    retry_events[target_aid].set()
                return _on_retry_finish

            await enqueue_solve_flow(
                send_func=batch_send,
                tarefa=aid,
                instrucoes=instrucoes,
                extra_files=combined_files,
                is_refazer=False,
                modo=modo,
                requester=requester,
                channel=channel,
                silent_enqueue=True,
                on_finish=_make_retry_finish(aid)
            )

        for aid, ev in retry_events.items():
            try:
                await ev.wait()
            except Exception:
                pass

    # 4. Fase 3: Relatório Final Consolidado
    total = len(chosen_items)
    succeeded = [rec for rec in records.values() if rec["success"]]
    recovered = [rec for rec in records.values() if rec["success"] and rec["retried"]]
    first_try_success = [rec for rec in records.values() if rec["success"] and not rec["retried"]]
    remaining_failures = [rec for rec in records.values() if not rec["success"]]

    if not remaining_failures:
        final_title = "🎉 Lote de Atividades Concluído com Sucesso Total!"
        color = discord.Color.green()
    elif len(succeeded) > 0:
        final_title = "⚠️ Lote de Atividades Concluído com Algumas Pendências"
        color = discord.Color.orange()
    else:
        final_title = "❌ Falha no Processamento do Lote de Atividades"
        color = discord.Color.red()

    report_desc = [
        f"O processamento do lote solicitado por {user_mention} foi finalizado.\n",
        f"📊 **Estatísticas Gerais:**",
        f"• **Total de Atividades:** {total}",
        f"• ✅ **Sucesso:** {len(succeeded)} ({len(first_try_success)} na 1ª tentativa" + (f", {len(recovered)} na repescagem" if recovered else "") + ")",
        f"• ❌ **Falhas Restantes:** {len(remaining_failures)}"
    ]

    report_embed = discord.Embed(
        title=final_title,
        description="\n".join(report_desc),
        color=color,
        timestamp=datetime.now()
    )

    if succeeded:
        succ_lines = []
        for rec in succeeded:
            it = rec["item"]
            badge = " *(recuperada na repescagem)*" if rec["retried"] else ""
            succ_lines.append(f"• ✅ **{it.get('title', 'Atividade')}** ({clean_display_course(it.get('course', 'Geral'))}){badge}")
        report_embed.add_field(
            name=f"✔ Atividades Concluídas ({len(succeeded)})",
            value="\n".join(succ_lines[:15]),
            inline=False
        )

    if remaining_failures:
        fail_lines = []
        for rec in remaining_failures:
            it = rec["item"]
            raw_err = rec.get("error") or "Erro desconhecido"
            clean_err = _format_friendly_error(raw_err)
            fail_lines.append(f"• ❌ **{it.get('title', 'Atividade')}** ({clean_display_course(it.get('course', 'Geral'))})\n  ↳ *Motivo:* `{clean_err}`")
        report_embed.add_field(
            name=f"❌ Falhas Restantes ({len(remaining_failures)})",
            value="\n".join(fail_lines[:10]),
            inline=False
        )
        report_embed.add_field(
            name="💡 O que acontece com as atividades que falharam?",
            value=(
                "Essas tarefas **permanecem como pendentes** no sistema (não foram marcadas como concluídas).\n"
                "Você pode tentar resolvê-las novamente a qualquer momento com o comando `/resolver`."
            ),
            inline=False
        )

    report_embed.set_footer(text="Moodle Bot UFMG • Relatório de Lote Consolidado")

    try:
        await batch_send(embed=report_embed)
    except Exception:
        if channel and hasattr(channel, "send"):
            try:
                await channel.send(embed=report_embed)
            except Exception:
                pass


class BatchSelectView(ui.View):
    """Painel interativo para seleção de múltiplas tarefas pendentes, materiais de apoio e disparo em lote."""

    def __init__(
        self,
        pending_items: List[Dict[str, Any]],
        disciplina_filter: Optional[str] = None,
        instrucoes: Optional[str] = None,
        attached_files: Optional[List[Path]] = None,
        available_materials: Optional[List[Path]] = None,
        requester: str = "Usuário",
        timeout: Optional[float] = 300
    ):
        super().__init__(timeout=timeout)
        self.all_pending = pending_items
        self.disciplina_filter = disciplina_filter
        self.instrucoes = instrucoes
        self.attached_files = list(attached_files or [])
        self.available_materials = list(available_materials or [])
        self.selected_materials: List[Path] = []
        self.requester = requester
        self.selected_ids: List[str] = []

        # 1. Menu Dropdown de Seleção de Tarefas (Row 0)
        task_options: List[discord.SelectOption] = []
        for item in self.all_pending[:25]:
            aid = str(item.get("id", ""))
            title = item.get("title", f"Atividade {aid}")
            course = clean_display_course(item.get("course", "Geral"))
            due_str = item.get("due_date", "Sem prazo")
            is_quiz = "mod/quiz" in item.get("url", "").lower() or item.get("activity_type") == "quiz"
            prefix = "Quiz" if is_quiz else "Tarefa"

            label = f"[{prefix}] {title}"[:100]
            desc = f"{course} • Prazo: {due_str}"[:100]
            task_options.append(
                discord.SelectOption(
                    label=label,
                    value=aid,
                    description=desc,
                    emoji="📝" if is_quiz else "📄"
                )
            )

        max_picks = min(len(task_options), 25)
        self.task_select_menu = ui.Select(
            placeholder=f"Selecione de 1 a {max_picks} atividades para resolver em lote...",
            min_values=1,
            max_values=max_picks,
            options=task_options,
            row=0
        )
        self.task_select_menu.callback = self.on_select_tasks
        self.add_item(self.task_select_menu)

        # 2. Menu Dropdown de Seleção de Materiais Salvos (Row 1) se houver materiais disponíveis
        if self.available_materials:
            mat_options: List[discord.SelectOption] = []
            for p in self.available_materials[:25]:
                size_kb = p.stat().st_size // 1024 if p.exists() else 0
                size_str = f"{size_kb} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
                mat_options.append(
                    discord.SelectOption(
                        label=p.name[:100],
                        value=p.name,
                        description=f"Tamanho: {size_str}"[:100],
                        emoji="📚"
                    )
                )
            max_mat_picks = min(len(mat_options), 3)
            self.mat_select_menu = ui.Select(
                placeholder=f"Selecione até {max_mat_picks} materiais salvos de apoio para o lote...",
                min_values=0,
                max_values=max_mat_picks,
                options=mat_options,
                row=1
            )
            self.mat_select_menu.callback = self.on_select_materials
            self.add_item(self.mat_select_menu)
        else:
            self.mat_select_menu = None

    def build_panel_embed(self) -> discord.Embed:
        disc_info = f" da disciplina **{self.disciplina_filter}**" if self.disciplina_filter else ""
        embed = discord.Embed(
            title="📦 Resolução de Atividades em Lote",
            description=(
                f"Encontradas **{len(self.all_pending)} atividade(s) pendente(s)**{disc_info}.\n\n"
                "1. Marque no **menu de atividades** as que deseja incluir no lote;\n"
                "2. (Opcional) Escolha **materiais de apoio** ou clique em **[✏️ Instruções]**;\n"
                "3. Escolha o nível de autonomia nos botões para disparar a execução."
            ),
            color=discord.Color.blue()
        )

        if self.selected_ids:
            id_to_item = {str(item.get("id", "")): item for item in self.all_pending}
            sel_names = [f"`{id_to_item[aid].get('title', aid)}`" for aid in self.selected_ids if aid in id_to_item]
            prev = ", ".join(sel_names[:4])
            if len(sel_names) > 4:
                prev += f" (+{len(sel_names) - 4})"
            embed.add_field(name=f"📋 Atividades Selecionadas ({len(self.selected_ids)})", value=prev, inline=False)
        else:
            embed.add_field(name="📋 Atividades Selecionadas", value="*Nenhuma marcada ainda (use o dropdown acima)*", inline=False)

        combined_mats = list(self.attached_files)
        for sm in self.selected_materials:
            if sm not in combined_mats:
                combined_mats.append(sm)

        if combined_mats:
            mat_names = [f"`{p.name}`" for p in combined_mats]
            embed.add_field(name=f"📚 Materiais de Apoio para o Lote ({len(combined_mats)})", value="\n".join(f"• {n}" for n in mat_names[:5]), inline=False)
        else:
            embed.add_field(name="📚 Materiais de Apoio", value="*Nenhum arquivo externo (a IA usará apenas o enunciado de cada atividade)*", inline=False)

        if self.instrucoes:
            embed.add_field(name="📝 Instruções da IA para o Lote", value=f"```\n{self.instrucoes[:400]}\n```", inline=False)

        embed.set_footer(text="A execução em lote é estritamente sequencial (FIFO) para segurança do Moodle.")
        return embed

    async def on_select_tasks(self, interaction: discord.Interaction):
        self.selected_ids = list(self.task_select_menu.values)
        embed = self.build_panel_embed()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            pass

    async def on_select_materials(self, interaction: discord.Interaction):
        chosen_names = set(self.mat_select_menu.values)
        self.selected_materials = [p for p in self.available_materials if p.name in chosen_names]
        embed = self.build_panel_embed()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            pass


    async def _dispatch_batch(self, interaction: discord.Interaction, modo: str):
        chosen_ids = list(self.task_select_menu.values) if self.task_select_menu.values else self.selected_ids
        if not chosen_ids:
            await interaction.response.send_message(
                "⚠️ **Nenhuma atividade foi selecionada!**\nAbra o menu dropdown acima e escolha pelo menos uma atividade.",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        for child in self.children:
            child.disabled = True

        id_to_item = {str(item.get("id", "")): item for item in self.all_pending}
        chosen_items = [id_to_item[aid] for aid in chosen_ids if aid in id_to_item]

        combined_files = list(self.attached_files)
        for sm in self.selected_materials:
            if sm not in combined_files:
                combined_files.append(sm)

        mode_labels = {
            "resolver": "🧠 Apenas Resolver (Gera rascunhos para conferência)",
            "preencher": "📝 Resolver e Preencher (Preenche no Moodle sem submeter)",
            "finalizar": "⚡ Resolver, Preencher e Enviar Tudo (End-to-End)"
        }
        mode_label = mode_labels.get(modo, modo)

        summary_lines = []
        channel = interaction.channel
        ch_id = getattr(interaction, "channel_id", None) or getattr(channel, "id", None)
        if not channel and ch_id:
            channel = bot.get_channel(ch_id)
        if not channel and ch_id and getattr(bot.http, "token", None):
            try:
                channel = await bot.fetch_channel(ch_id)
            except Exception:
                pass
        if not channel and settings.DISCORD_CHANNEL_ID:
            channel = bot.get_channel(settings.DISCORD_CHANNEL_ID)
            if not channel and getattr(bot.http, "token", None):
                try:
                    channel = await bot.fetch_channel(settings.DISCORD_CHANNEL_ID)
                except Exception:
                    pass

        # Auto-detecta e direciona para o canal 'fila-tarefas' da categoria do usuário se existir
        category = getattr(channel, "category", None)
        if category:
            for cat_ch in getattr(category, "text_channels", []):
                if "fila" in cat_ch.name.lower():
                    queue_manager.set_dashboard_channel(cat_ch.id)
                    break

        batch_send = channel.send if (channel and hasattr(channel, "send")) else interaction.followup.send
        requester_str = interaction.user.display_name if interaction.user else self.requester
        user_mention = interaction.user.mention if interaction.user else self.requester

        records = {}
        done_events = {}

        for item in chosen_items:
            aid = str(item.get("id", ""))
            done_events[aid] = asyncio.Event()
            records[aid] = {
                "item": item,
                "success": False,
                "message": "",
                "error": None,
                "retried": False
            }

            def _make_pass1_finish(target_aid=aid):
                async def _on_finish(success: bool, msg: str):
                    records[target_aid]["success"] = success
                    records[target_aid]["message"] = msg
                    if not success:
                        records[target_aid]["error"] = msg
                    done_events[target_aid].set()
                return _on_finish

            pos = await enqueue_solve_flow(
                send_func=batch_send,
                tarefa=aid,
                instrucoes=self.instrucoes,
                extra_files=combined_files,
                is_refazer=False,
                modo=modo,
                requester=requester_str,
                channel=channel,
                silent_enqueue=True,
                on_finish=_make_pass1_finish(aid)
            )
            summary_lines.append(f"• **#{pos}** na fila: `{item.get('title', aid)}` ({clean_display_course(item.get('course', 'Geral'))})")

        effective_queue_id = queue_manager._dashboard_channel_id or settings.DISCORD_QUEUE_CHANNEL_ID
        queue_mention = f"<#{effective_queue_id}>" if effective_queue_id else "canal `#fila-de-tarefas`"
        embed = discord.Embed(
            title="📦 Lote de Atividades Enfileirado com Sucesso!",
            description=(
                f"Foram agendadas **{len(chosen_items)} atividade(s)** para execução sequencial.\n\n"
                f"⚙️ **Modo Escolhido:** `{mode_label}`\n"
                f"👤 **Solicitante:** {user_mention}\n"
                f"📍 **Acompanhamento:** Verifique a ordem e o andamento em tempo real no {queue_mention}.\n\n"
                "**Ordem de Execução na Fila:**\n" + "\n".join(summary_lines[:15])
            ),
            color=discord.Color.green()
        )
        if combined_files:
            embed.add_field(
                name="📚 Materiais de Referência Aplicados a Todo o Lote",
                value="\n".join(f"• `{p.name}`" for p in combined_files[:5]),
                inline=False
            )
        if self.instrucoes:
            embed.add_field(
                name="📝 Instruções da IA",
                value=f"```\n{self.instrucoes[:400]}\n```",
                inline=False
            )
        if len(summary_lines) > 15:
            embed.set_footer(text=f"... e mais {len(summary_lines) - 15} atividades enfileiradas.")

        try:
            if hasattr(interaction, "message") and interaction.message:
                await interaction.message.edit(view=self)
        except Exception:
            pass

        await interaction.followup.send(embed=embed)

        # Inicia coordenador em segundo plano para auto-retry e relatório final
        asyncio.create_task(
            _coordinate_batch_followup(
                records=records,
                done_events=done_events,
                chosen_items=chosen_items,
                batch_send=batch_send,
                modo=modo,
                instrucoes=self.instrucoes,
                combined_files=combined_files,
                requester=requester_str,
                channel=channel,
                user_mention=user_mention
            )
        )

    @ui.button(label="Instruções", style=discord.ButtonStyle.secondary, emoji="✏️", row=2)
    async def btn_instrucoes(self, interaction: discord.Interaction, button: ui.Button):
        modal = BatchInstructionModal(parent_view=self)
        await interaction.response.send_modal(modal)

    @ui.button(label="Apenas Resolver", style=discord.ButtonStyle.primary, emoji="🧠", row=2)
    async def btn_resolver(self, interaction: discord.Interaction, button: ui.Button):
        await self._dispatch_batch(interaction, modo="resolver")

    @ui.button(label="Resolver e Preencher", style=discord.ButtonStyle.primary, emoji="📝", row=2)
    async def btn_preencher(self, interaction: discord.Interaction, button: ui.Button):
        await self._dispatch_batch(interaction, modo="preencher")

    @ui.button(label="Resolver e Enviar Tudo", style=discord.ButtonStyle.success, emoji="⚡", row=3)
    async def btn_finalizar(self, interaction: discord.Interaction, button: ui.Button):
        await self._dispatch_batch(interaction, modo="finalizar")

    @ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="❌", row=3)
    async def btn_cancelar(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(interaction, "message") and interaction.message:
                await interaction.message.edit(view=self)
        except Exception:
            pass
        await interaction.response.send_message("❌ Execução em lote cancelada pelo usuário.", ephemeral=True)


@bot.tree.command(name="resolver_lote", description="Seleciona e resolve múltiplas tarefas/questionários pendentes em lote")
@app_commands.describe(
    disciplina="Filtrar atividades pendentes por disciplina específica (opcional)",
    instrucoes="Instruções adicionais personalizadas para todas as tarefas do lote (opcional)",
    arquivo="Arquivo complementar anexado por você (gabarito, PDF, foto) para usar no lote (opcional)",
    material_1="Material 1 salvo da matéria para usar como apoio no lote (opcional)",
    material_2="Material 2 salvo da matéria para usar como apoio no lote (opcional)",
    material_3="Material 3 salvo da matéria para usar como apoio no lote (opcional)"
)
@app_commands.autocomplete(
    disciplina=course_autocomplete,
    material_1=task_material_autocomplete,
    material_2=task_material_autocomplete,
    material_3=task_material_autocomplete
)
async def cmd_resolver_lote(
    interaction: discord.Interaction,
    disciplina: Optional[str] = None,
    instrucoes: Optional[str] = None,
    arquivo: Optional[discord.Attachment] = None,
    material_1: Optional[str] = None,
    material_2: Optional[str] = None,
    material_3: Optional[str] = None
):
    await interaction.response.defer(ephemeral=False)
    assignments = await _get_current_assignments()

    extra_files: List[Path] = []
    if arquivo:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest_file = temp_dir / sanitize_filename(arquivo.filename)
        await arquivo.save(dest_file)
        extra_files.append(dest_file)

    pending_items = []
    norm_disc = normalize_text(disciplina) if disciplina else None

    for aid, item in assignments.items():
        if _is_task_completed(item):
            continue
        if norm_disc:
            item_course_norm = normalize_text(item.get("course", ""))
            if norm_disc not in item_course_norm:
                continue
        pending_items.append(item)

    def _sort_key(it):
        return (it.get("due_date", "9999"), natural_sort_key(it.get("title", "")))

    pending_items.sort(key=_sort_key)

    if not pending_items:
        msg = "🎉 Nenhuma atividade pendente encontrada"
        if disciplina:
            msg += f" para a matéria **{disciplina}**."
        else:
            msg += " no catálogo local do Moodle."
        await interaction.followup.send(msg)
        return

    # Coleta materiais disponíveis para a disciplina ou tarefas pendentes encontradas
    available_mats: List[Path] = []
    seen_mats = set()
    if disciplina:
        for p in get_course_materials_for_task(disciplina):
            if p.name not in seen_mats:
                seen_mats.add(p.name)
                available_mats.append(p)
    for it in pending_items[:25]:
        for p in get_course_materials_for_task(str(it.get("id", ""))):
            if p.name not in seen_mats:
                seen_mats.add(p.name)
                available_mats.append(p)

    # Se usuário especificou material_1/2/3 diretamente nos parâmetros
    specified_mats = [m for m in [material_1, material_2, material_3] if m]
    if specified_mats:
        for m_name in specified_mats:
            for p in available_mats:
                if p.name == m_name or normalize_text(p.name) == normalize_text(m_name):
                    if p not in extra_files:
                        extra_files.append(p)
                    break

    view = BatchSelectView(
        pending_items=pending_items,
        disciplina_filter=disciplina,
        instrucoes=instrucoes,
        attached_files=extra_files,
        available_materials=available_mats,
        requester=interaction.user.display_name if interaction.user else "Usuário"
    )
    embed = view.build_panel_embed()
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="status", description="Exibe o status da sessão Moodle, materiais e modelos de IA")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    embed = await build_status_embed()
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="adicionarconteudo", description="Salva arquivos e resumos na base de conhecimento da matéria")
@app_commands.describe(
    disciplina="Disciplina que receberá o material",
    arquivo="Arquivo a ser salvo (PDF, resumo, slide, lista)"
)
@app_commands.autocomplete(disciplina=course_autocomplete)
async def cmd_adicionarconteudo(
    interaction: discord.Interaction,
    disciplina: str,
    arquivo: discord.Attachment
):
    await interaction.response.defer(ephemeral=False)

    content_ch_id = settings.DISCORD_CONTENT_CHANNEL_ID
    ch_name = getattr(interaction.channel, "name", "")
    if content_ch_id and content_ch_id != 0 and interaction.channel_id != content_ch_id and ch_name != "conteudos":
        await interaction.followup.send(
            f"⚠️ Este comando deve ser executado no canal dedicado a conteúdos: <#{content_ch_id}>.",
            ephemeral=True
        )
        return

    dest_dir = resolve_course_materials_dir(disciplina)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / sanitize_filename(arquivo.filename)
    await arquivo.save(dest_file)

    console.print(f"[green]✔ Novo conteúdo adicionado via Discord:[/green] {dest_file.name} em {dest_dir.name}")

    embed = discord.Embed(
        title="📥 Conteúdo Adicionado à Base de Conhecimento!",
        description=f"O arquivo **`{arquivo.filename}`** foi salvo com sucesso.",
        color=discord.Color.green()
    )
    embed.add_field(name="🏫 Disciplina", value=dest_dir.name, inline=True)
    embed.add_field(name="📦 Tamanho", value=f"{arquivo.size // 1024} KB", inline=True)
    embed.set_footer(text="A IA passará a considerar este documento nas próximas resoluções.")

    # Anexa o arquivo na confirmação para persistência permanente no CDN do Discord
    discord_file = discord.File(str(dest_file), filename=dest_file.name)
    followup_msg = await interaction.followup.send(embed=embed, file=discord_file)

    att_url = followup_msg.attachments[0].url if getattr(followup_msg, "attachments", None) else arquivo.url
    msg_id = getattr(followup_msg, "id", None)
    ch_id = getattr(interaction.channel, "id", None)

    # 1. Registra no DaemonState local
    try:
        from src.scheduler.state import DaemonState
        state = DaemonState()
        state.register_custom_material(
            course=dest_dir.name,
            filename=dest_file.name,
            attachment_url=att_url,
            channel_id=ch_id,
            message_id=msg_id,
            uploader=str(interaction.user),
            size=arquivo.size
        )
    except Exception as st_err:
        console.print(f"[yellow]Aviso ao registrar material no state: {st_err}[/yellow]")

    # 2. Registra na Cloud Bridge para sincronização imediata com desktop
    try:
        from src.notifier.bridge_manager import cloud_bridge
        await cloud_bridge.register_material({
            "course": dest_dir.name,
            "filename": dest_file.name,
            "attachment_url": att_url,
            "channel_id": ch_id,
            "message_id": msg_id,
            "uploader": str(interaction.user),
            "size": arquivo.size
        })
    except Exception as br_err:
        console.print(f"[yellow]Aviso ao registrar material na Cloud Bridge: {br_err}[/yellow]")


@bot.tree.command(name="notion_adicionar", description="Adiciona uma tarefa, estudo ou anotação ao Notion e anuncia no Discord")
@app_commands.describe(
    titulo="Título da tarefa ou anotação a ser adicionada",
    disciplina="Disciplina relacionada (ex: Eletromagnetismo)",
    prazo="Data ou prazo de estudo (formato AAAA-MM-DD ou DD/MM/AAAA)",
    tipo="Tipo do registro (TAREFA, ESTUDO, TRABALHO, PROVA ou OUTROS)",
    detalhes="Detalhes, passos de estudo ou descrição completa"
)
@app_commands.autocomplete(disciplina=course_autocomplete)
async def cmd_notion_adicionar(
    interaction: discord.Interaction,
    titulo: str,
    disciplina: Optional[str] = None,
    prazo: Optional[str] = None,
    tipo: Optional[str] = "TAREFA✅",
    detalhes: Optional[str] = None
):
    """Comando para adicionar manualmente qualquer item ao Notion e anunciar no canal de avisos."""
    await interaction.response.defer(ephemeral=True)
    from src.notifier.notion_client import notion_client
    if not notion_client.is_configured:
        await interaction.followup.send("❌ Integração com Notion não está configurada no `.env` (verifique `NOTION_API_KEY`).", ephemeral=True)
        return

    # Normaliza prazo para formato ISO
    date_val = None
    if prazo:
        prazo_clean = prazo.strip()
        if "/" in prazo_clean:
            parts = prazo_clean.split("/")
            if len(parts) == 3:
                date_val = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        else:
            date_val = prazo_clean

    chosen_type = tipo.strip() if tipo else "TAREFA✅"
    if not any(k in chosen_type for k in ["PROVA", "TRABALHO", "TAREFA", "OUTROS"]):
        chosen_type = "TAREFA✅"

    res = await notion_client.create_task(
        title=titulo,
        date_str=date_val,
        category=chosen_type,
        course_name=disciplina,
        details=detalhes,
        notes_val=detalhes[:200] if detalhes else None,
        notify_discord=True
    )

    if res.get("success"):
        url = res.get("url", "")
        await interaction.followup.send(
            f"✔ **Item adicionado com sucesso ao Notion!**\n"
            f"🔗 [Abrir no Notion]({url})\n"
            f"📢 Notificação detalhada enviada para o canal de avisos da turma.",
            ephemeral=True
        )
    else:
        await interaction.followup.send(f"❌ Erro ao adicionar ao Notion: {res.get('error', 'Erro desconhecido')}", ephemeral=True)


@bot.tree.command(name="notion_sync", description="Sincroniza tarefas e listas pendentes do Moodle com o Notion e avisa no canal")
async def cmd_notion_sync(interaction: discord.Interaction):
    """Sincroniza as tarefas atuais pendentes do catálogo Moodle diretamente para o Notion."""
    await interaction.response.defer(ephemeral=True)
    from src.notifier.notion_client import notion_client
    if not notion_client.is_configured:
        await interaction.followup.send("❌ Integração com Notion não está configurada no `.env`.", ephemeral=True)
        return

    state = DaemonState()
    assignments = state.get_pending_assignments()
    if not assignments:
        await interaction.followup.send("ℹ Nenhuma tarefa pendente no catálogo local para sincronizar.", ephemeral=True)
        return

    added = 0
    already = 0
    for a in assignments:
        # Só sincroniza atividades que possuem data/prazo definido no Moodle
        if not a.due_date:
            continue

        tid = f"moodle_{a.id}"
        date_iso = a.due_date.strftime("%Y-%m-%d")

        r = await notion_client.create_task(
            title=a.title,
            date_str=date_iso,
            category="TAREFA✅" if getattr(a, "activity_type", "assign") != "quiz" else "TRABALHO🟡",
            course_name=a.course_name,
            task_id_val=tid,
            notes_val=f"Atividade Moodle: {a.title} ({a.course_name})",
            details=f"Atividade do Moodle com vencimento em {a.due_date_str or 'Data não informada'}.\nStatus no Moodle: {a.status_text or 'Pendente'}",
            moodle_url=a.url,
            steps=[
                f"Revisar conceitos e anotações de {a.course_name}",
                f"Resolver '{a.title}'",
                "Conferir envio no Moodle"
            ],
            notify_discord=True
        )
        if r.get("success"):
            if r.get("already_exists"):
                already += 1
            else:
                added += 1

    await interaction.followup.send(
        f"✔ **Sincronização com o Notion concluída!**\n"
        f"• Novos itens adicionados e anunciados: **{added}**\n"
        f"• Já sincronizados anteriormente: **{already}**",
        ephemeral=True
    )


@bot.tree.command(name="atualizar_checklist", description="Atualiza a checklist de tarefas do dia no Notion com a rotina e pendências")
async def cmd_atualizar_checklist(interaction: discord.Interaction):
    """Atualiza a checklist diária no bloco 'tarefas do dia' no Notion e anuncia no Discord."""
    await interaction.response.defer(ephemeral=True)
    from src.notifier.notion_client import notion_client
    if not notion_client.is_configured:
        await interaction.followup.send("❌ Integração com Notion não está configurada no `.env`.", ephemeral=True)
        return

    res = await notion_client.update_daily_checklist(notify_discord=True)
    if res.get("success"):
        tasks_list = res.get("tasks", [])
        tasks_preview = "\n".join([f"• {t}" for t in tasks_list[:8]])
        if len(tasks_list) > 8:
            tasks_preview += f"\n• ... e mais {len(tasks_list) - 8} itens"

        await interaction.followup.send(
            f"✔ **Checklist do dia atualizada no Notion com sucesso!**\n"
            f"📅 **Data:** {res.get('day_name')}, {res.get('date')}\n"
            f"📋 **Total de tarefas inseridas:** {res.get('tasks_count')}\n\n"
            f"**Prévia das tarefas:**\n{tasks_preview}",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"❌ Erro ao atualizar checklist no Notion: {res.get('error', 'Erro desconhecido')}",
            ephemeral=True
        )


def get_study_target_channel(interaction_or_ctx) -> Tuple[Any, bool]:
    """Retorna o canal alvo para atividades de estudo e se houve redirecionamento."""
    study_id = getattr(settings, "DISCORD_STUDY_CHANNEL_ID", 0)
    current_channel = getattr(interaction_or_ctx, "channel", None)
    if study_id and study_id > 0:
        target = bot.get_channel(study_id)
        if target:
            if current_channel and current_channel.id != study_id:
                return target, True
            return target, False
    return current_channel, False


class FlashcardsCarouselView(ui.View):
    """View interativa em carrossel para navegação e estudo de flashcards do Anki."""

    def __init__(
        self,
        cards: List[Dict[str, str]],
        discipline: str,
        topic: str,
        requester: str = "Estudante",
        timeout: Optional[float] = 900
    ):
        super().__init__(timeout=timeout)
        self.cards = cards or []
        self.discipline = discipline
        self.topic = topic
        self.requester = requester
        self.current_idx = 0
        self.is_flipped = False
        self._update_buttons()

    def _update_buttons(self):
        total = len(self.cards)
        self.btn_prev.disabled = (self.current_idx <= 0)
        self.btn_next.disabled = (self.current_idx >= total - 1)
        if self.is_flipped:
            self.btn_flip.label = "Ocultar Resposta"
            self.btn_flip.emoji = "🔄"
            self.btn_flip.style = discord.ButtonStyle.secondary
        else:
            self.btn_flip.label = "Revelar Resposta"
            self.btn_flip.emoji = "👁️"
            self.btn_flip.style = discord.ButtonStyle.primary

    def build_embed(self) -> discord.Embed:
        if not self.cards:
            return discord.Embed(
                title="🗂️ Baralho de Flashcards Vazio",
                description="Nenhum card foi gerado para este tópico.",
                color=discord.Color.red()
            )

        total = len(self.cards)
        card = self.cards[self.current_idx]
        disc_clean = clean_display_course(self.discipline)

        embed = discord.Embed(
            title=f"🗂️ Flashcards: {disc_clean}",
            description=f"**Tópico:** `{self.topic}` • **Card:** `{self.current_idx + 1}/{total}`",
            color=discord.Color.purple() if not self.is_flipped else discord.Color.green()
        )

        embed.add_field(name="❓ Pergunta / Conceito", value=f"**{card['front']}**", inline=False)

        if self.is_flipped:
            embed.add_field(name="💡 Resposta do Professor", value=card.get("back", ""), inline=False)
            if card.get("explanation"):
                embed.add_field(name="⚠️ Dica & Pegadinha", value=card["explanation"], inline=False)
            if card.get("source"):
                embed.add_field(name="📚 Referência nos Slides", value=f"`{card['source']}`", inline=False)
        else:
            embed.add_field(name="🔒 Resposta Oculta", value="*Pense na resposta e clique em `[👁️ Revelar Resposta]` abaixo.*", inline=False)

        embed.set_footer(text=f"Solicitado por {self.requester} • Arquivo Anki pronto para importação em anexo!")
        return embed

    @ui.button(label="Anterior", style=discord.ButtonStyle.secondary, emoji="⬅️", row=0)
    async def btn_prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_idx > 0:
            self.current_idx -= 1
            self.is_flipped = False
            self._update_buttons()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    @ui.button(label="Revelar Resposta", style=discord.ButtonStyle.primary, emoji="👁️", row=0)
    async def btn_flip(self, interaction: discord.Interaction, button: ui.Button):
        self.is_flipped = not self.is_flipped
        self._update_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Próximo", style=discord.ButtonStyle.secondary, emoji="➡️", row=0)
    async def btn_next(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_idx < len(self.cards) - 1:
            self.current_idx += 1
            self.is_flipped = False
            self._update_buttons()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()


class InteractiveQuizSessionView(ui.View):
    """View interativa para simulado pré-prova no Discord com avaliação instantânea e pegadinhas."""

    def __init__(
        self,
        questions: List[Dict[str, Any]],
        discipline: str,
        topic: str,
        requester: str = "Estudante",
        requester_id: Optional[int] = None,
        timeout: Optional[float] = 900
    ):
        super().__init__(timeout=timeout)
        self.questions = questions or []
        self.discipline = discipline
        self.topic = topic
        self.requester = requester
        self.requester_id = requester_id
        self.current_idx = 0
        self.score = 0
        self.user_answers: Dict[int, str] = {}
        self.is_answered = False
        self._configure_for_current_question()

    def _configure_for_current_question(self):
        self.is_answered = (self.current_idx in self.user_answers)
        is_last = (self.current_idx == len(self.questions) - 1)

        for opt_char in ["A", "B", "C", "D"]:
            btn = getattr(self, f"btn_opt_{opt_char.lower()}", None)
            if btn:
                if self.is_answered:
                    btn.disabled = True
                    chosen = self.user_answers.get(self.current_idx)
                    correct = self.questions[self.current_idx].get("correct_option", "A")
                    if opt_char == correct:
                        btn.style = discord.ButtonStyle.success
                    elif opt_char == chosen:
                        btn.style = discord.ButtonStyle.danger
                    else:
                        btn.style = discord.ButtonStyle.secondary
                else:
                    btn.disabled = False
                    btn.style = discord.ButtonStyle.primary

        if hasattr(self, "btn_next_action"):
            if not self.is_answered:
                self.btn_next_action.disabled = True
                self.btn_next_action.label = "Próxima Questão" if not is_last else "Finalizar Simulado"
                self.btn_next_action.style = discord.ButtonStyle.secondary
            else:
                self.btn_next_action.disabled = False
                self.btn_next_action.label = "Próxima Questão ➡️" if not is_last else "Ver Resultado Final 🏁"
                self.btn_next_action.style = discord.ButtonStyle.success

    def build_question_embed(self) -> discord.Embed:
        if not self.questions:
            return discord.Embed(title="Simulado Vazio", description="Nenhuma questão gerada.", color=discord.Color.red())

        q = self.questions[self.current_idx]
        total = len(self.questions)
        disc_clean = clean_display_course(self.discipline)

        embed = discord.Embed(
            title=f"📝 Simulado Pré-Prova: {disc_clean}",
            description=(
                f"**Tema:** `{self.topic}` • **Questão {self.current_idx + 1} de {total}**\n\n"
                f"### {q['question']}\n\n"
                f"**[A]** {q['options'].get('A', '')}\n"
                f"**[B]** {q['options'].get('B', '')}\n"
                f"**[C]** {q['options'].get('C', '')}\n"
                f"**[D]** {q['options'].get('D', '')}\n"
            ),
            color=discord.Color.blue()
        )

        if self.is_answered:
            chosen = self.user_answers[self.current_idx]
            correct = q.get("correct_option", "A")
            if chosen == correct:
                embed.color = discord.Color.green()
                embed.add_field(
                    name="🎉 Parabéns! Você Acertou!",
                    value=f"Alternativa correta: **[{correct}]**",
                    inline=False
                )
            else:
                embed.color = discord.Color.red()
                embed.add_field(
                    name="❌ Atenção à Pegadinha!",
                    value=f"Você marcou **[{chosen}]**, mas a alternativa correta é **[{correct}]**.",
                    inline=False
                )

            embed.add_field(name="💡 Explicação Pedagógica", value=q.get("explanation", "Sem explicação"), inline=False)
            if q.get("reference"):
                embed.add_field(name="📚 Referência nos Slides", value=f"`{q['reference']}`", inline=False)
        else:
            embed.set_footer(text=f"Pontuação atual: {self.score}/{self.current_idx} acertos • Escolha uma opção abaixo")

        return embed

    def build_results_embed(self) -> discord.Embed:
        total = len(self.questions)
        pct = (self.score / total * 100) if total > 0 else 0
        disc_clean = clean_display_course(self.discipline)

        if pct >= 80:
            status_text = "🏆 **Desempenho Excepcional!** Você dominou os conceitos e superou as pegadinhas!"
            color = discord.Color.green()
        elif pct >= 50:
            status_text = "📚 **Bom Desempenho!** Você está no caminho certo, mas vale revisar as pegadinhas das aulas."
            color = discord.Color.gold()
        else:
            status_text = "⚠️ **Atenção aos Conceitos!** Recomendamos reler os slides indicados e usar `/flashcards` para fixar."
            color = discord.Color.orange()

        embed = discord.Embed(
            title=f"🏁 Simulado Concluído: {disc_clean}",
            description=(
                f"### Placar Final: **{self.score} de {total} questões corretas ({pct:.0f}%)**\n\n"
                f"{status_text}\n\n"
                f"👤 **Estudante:** {self.requester}\n"
                f"🎯 **Tópico:** `{self.topic}`"
            ),
            color=color
        )
        embed.set_footer(text="Quer treinar novamente? Clique no botão abaixo!")
        return embed

    async def _handle_option_click(self, interaction: discord.Interaction, chosen: str):
        if self.requester_id and interaction.user.id != self.requester_id:
            await interaction.response.send_message("⚠️ Este simulado pertence a outro estudante. Inicie o seu com `/quiz`!", ephemeral=True)
            return

        if self.is_answered:
            await interaction.response.defer()
            return

        correct = self.questions[self.current_idx].get("correct_option", "A")
        self.user_answers[self.current_idx] = chosen
        if chosen == correct:
            self.score += 1

        self.is_answered = True
        self._configure_for_current_question()
        embed = self.build_question_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="A", style=discord.ButtonStyle.primary, row=0)
    async def btn_opt_a(self, interaction: discord.Interaction, button: ui.Button):
        await self._handle_option_click(interaction, "A")

    @ui.button(label="B", style=discord.ButtonStyle.primary, row=0)
    async def btn_opt_b(self, interaction: discord.Interaction, button: ui.Button):
        await self._handle_option_click(interaction, "B")

    @ui.button(label="C", style=discord.ButtonStyle.primary, row=0)
    async def btn_opt_c(self, interaction: discord.Interaction, button: ui.Button):
        await self._handle_option_click(interaction, "C")

    @ui.button(label="D", style=discord.ButtonStyle.primary, row=0)
    async def btn_opt_d(self, interaction: discord.Interaction, button: ui.Button):
        await self._handle_option_click(interaction, "D")

    @ui.button(label="Próxima Questão", style=discord.ButtonStyle.secondary, emoji="➡️", row=1)
    async def btn_next_action(self, interaction: discord.Interaction, button: ui.Button):
        if self.requester_id and interaction.user.id != self.requester_id:
            await interaction.response.send_message("⚠️ Este simulado pertence a outro estudante.", ephemeral=True)
            return

        if self.current_idx < len(self.questions) - 1:
            self.current_idx += 1
            self.is_answered = False
            self._configure_for_current_question()
            embed = self.build_question_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            for child in self.children:
                if child != self.btn_restart:
                    child.disabled = True
            self.btn_restart.disabled = False
            embed = self.build_results_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Refazer Simulado", style=discord.ButtonStyle.primary, emoji="🔄", row=1, disabled=True)
    async def btn_restart(self, interaction: discord.Interaction, button: ui.Button):
        if self.requester_id and interaction.user.id != self.requester_id:
            await interaction.response.send_message("⚠️ Este simulado pertence a outro estudante.", ephemeral=True)
            return

        self.current_idx = 0
        self.score = 0
        self.user_answers.clear()
        self.is_answered = False
        self.btn_restart.disabled = True
        self._configure_for_current_question()
        embed = self.build_question_embed()
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="perguntar", description="Tira dúvidas conceituais com a IA citando slides e apostilas da disciplina")
@app_commands.describe(
    disciplina="Nome da disciplina (ex: Cálculo, Física, Inglês)",
    duvida="Sua dúvida conceitual, fórmula ou questão teórica",
    material="Material específico da disciplina para consulta prioritária (opcional)"
)
@app_commands.autocomplete(
    disciplina=course_autocomplete,
    material=task_material_autocomplete
)
async def cmd_perguntar(
    interaction: discord.Interaction,
    disciplina: str,
    duvida: str,
    material: Optional[str] = None
):
    target_ch, redirected = get_study_target_channel(interaction)
    if redirected:
        await interaction.response.send_message(
            f"📍 Sua dúvida sobre **{disciplina}** foi encaminhada para o canal de estudos: <#{target_ch.id}>!",
            ephemeral=True
        )
    else:
        await interaction.response.defer(ephemeral=False)

    if _is_relay_mode() and not _has_ai_key_configured():
        if redirected:
            await target_ch.send(content=f"{interaction.user.mention}\n{BYOK_RELAY_MESSAGE}")
        else:
            await interaction.followup.send(content=BYOK_RELAY_MESSAGE)
        return

    tutor = StudyTutor()
    res = await tutor.answer_question(discipline=disciplina, question=duvida, specific_material=material)

    disc_clean = clean_display_course(disciplina)
    embed = discord.Embed(
        title=f"💡 Tutor Acadêmico: {disc_clean}",
        color=discord.Color.blue()
    )
    embed.add_field(name="❓ Dúvida do Aluno", value=f"*{duvida[:500]}*", inline=False)

    ans_text = res.get("answer", "")
    if len(ans_text) <= 4000:
        embed.description = f"### 📖 Resposta do Tutor\n\n{ans_text}"
    else:
        embed.description = f"### 📖 Resposta do Tutor\n\n{ans_text[:3900]}\n\n*(continua no próximo campo...)*"
        embed.add_field(name="📖 Continuação", value=ans_text[3900:4900], inline=False)

    mats = res.get("materials_used", [])
    if mats:
        embed.add_field(name="📚 Materiais & Slides Consultados", value="\n".join(f"• `{m}`" for m in mats[:4]), inline=False)

    embed.set_footer(text=f"Solicitado por {interaction.user.display_name} • Modelo: {res.get('model_used')}")

    if redirected:
        await target_ch.send(content=f"{interaction.user.mention} aqui está a resposta para a sua dúvida:", embed=embed)
    else:
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="flashcards", description="Gera baralho de flashcards para estudo ativo com exportação direta para o Anki")
@app_commands.describe(
    disciplina="Nome da disciplina (ex: Cálculo, Química, Inglês)",
    topico="Tópico específico a ser enfatizado (opcional)",
    qtd="Quantidade de flashcards a gerar (3 a 15, padrão: 8)",
    material="Material específico da disciplina para basear o baralho (opcional)"
)
@app_commands.autocomplete(
    disciplina=course_autocomplete,
    material=task_material_autocomplete
)
async def cmd_flashcards(
    interaction: discord.Interaction,
    disciplina: str,
    topico: Optional[str] = None,
    qtd: Optional[int] = 8,
    material: Optional[str] = None
):
    target_ch, redirected = get_study_target_channel(interaction)
    if redirected:
        await interaction.response.send_message(
            f"📍 Seu baralho de flashcards para **{disciplina}** está sendo gerado no canal de estudos: <#{target_ch.id}>!",
            ephemeral=True
        )
    else:
        await interaction.response.defer(ephemeral=False)

    if _is_relay_mode() and not _has_ai_key_configured():
        if redirected:
            await target_ch.send(content=f"{interaction.user.mention}\n{BYOK_RELAY_MESSAGE}")
        else:
            await interaction.followup.send(content=BYOK_RELAY_MESSAGE)
        return

    tutor = StudyTutor()
    count = max(3, min(qtd or 8, 15))
    res = await tutor.generate_flashcards(discipline=disciplina, topic=topico, count=count, specific_material=material)

    view = FlashcardsCarouselView(
        cards=res.get("cards", []),
        discipline=disciplina,
        topic=res.get("topic", "Geral"),
        requester=interaction.user.display_name
    )
    embed = view.build_embed()

    anki_path = res.get("anki_file_path")
    file_to_send = None
    if anki_path and Path(anki_path).exists():
        file_to_send = discord.File(str(anki_path), filename=Path(anki_path).name)

    if redirected:
        if file_to_send:
            await target_ch.send(
                content=f"{interaction.user.mention} aqui está o seu baralho de flashcards interativo e o arquivo Anki!",
                embed=embed,
                view=view,
                file=file_to_send
            )
        else:
            await target_ch.send(
                content=f"{interaction.user.mention} aqui está o seu baralho de flashcards interativo!",
                embed=embed,
                view=view
            )
    else:
        if file_to_send:
            await interaction.followup.send(embed=embed, view=view, file=file_to_send)
        else:
            await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="quiz", description="Gera simulado pré-prova interativo com botões A, B, C, D e pegadinhas reais")
@app_commands.describe(
    disciplina="Nome da disciplina (ex: Cálculo, Fundamentos de Eletromag., Inglês)",
    qtd_questoes="Quantidade de questões no simulado (3 a 10, padrão: 5)",
    topico="Tópico específico da matéria para focar o simulado (opcional)",
    material="Material específico da disciplina para consulta prioritária (opcional)"
)
@app_commands.autocomplete(
    disciplina=course_autocomplete,
    material=task_material_autocomplete
)
async def cmd_quiz(
    interaction: discord.Interaction,
    disciplina: str,
    qtd_questoes: Optional[int] = 5,
    topico: Optional[str] = None,
    material: Optional[str] = None
):
    target_ch, redirected = get_study_target_channel(interaction)
    if redirected:
        await interaction.response.send_message(
            f"📍 Seu simulado para **{disciplina}** foi iniciado no canal de estudos: <#{target_ch.id}>!",
            ephemeral=True
        )
    else:
        await interaction.response.defer(ephemeral=False)

    if _is_relay_mode() and not _has_ai_key_configured():
        if redirected:
            await target_ch.send(content=f"{interaction.user.mention}\n{BYOK_RELAY_MESSAGE}")
        else:
            await interaction.followup.send(content=BYOK_RELAY_MESSAGE)
        return

    tutor = StudyTutor()
    num_q = max(3, min(qtd_questoes or 5, 10))
    res = await tutor.generate_quiz(discipline=disciplina, num_questions=num_q, topic=topico, specific_material=material)

    view = InteractiveQuizSessionView(
        questions=res.get("questions", []),
        discipline=disciplina,
        topic=res.get("topic", "Geral"),
        requester=interaction.user.display_name,
        requester_id=interaction.user.id
    )
    embed = view.build_question_embed()

    if redirected:
        await target_ch.send(
            content=f"{interaction.user.mention} seu simulado interativo começou! Responda nos botões abaixo:",
            embed=embed,
            view=view
        )
    else:
        await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="meuscanais", description="Cria ou localiza sua categoria e as 5 salas privadas do Moodle neste servidor")
async def cmd_meuscanais(interaction: discord.Interaction):
    """Cria ou recupera as salas privadas do usuário neste servidor."""
    if not interaction.guild:
        await interaction.response.send_message("❌ Este comando deve ser executado dentro de um servidor do Discord.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        res = await asyncio.wait_for(
            provision_user_channels(interaction.guild, interaction.user),
            timeout=20.0
        )
    except asyncio.TimeoutError:
        await interaction.followup.send(
            "⏱️ **Tempo esgotado.** O Discord demorou para criar os canais — tente novamente.\n"
            "Se o erro persistir, verifique se o bot tem permissão **Gerenciar Canais** no servidor.",
            ephemeral=True
        )
        return
    except discord.Forbidden:
        await interaction.followup.send(
            "🚫 **Sem permissão.** O bot precisa da permissão **Gerenciar Canais** no servidor para criar suas salas.\n"
            "Peça ao administrador do servidor para conceder essa permissão ao bot.",
            ephemeral=True
        )
        return
    ch_ids = res["channels"]

    embed = discord.Embed(
        title="🔒 Suas Salas Pessoais do Moodle Bot",
        description=(
            f"Categoria: **{res['category_name']}**\n\n"
            f"• 📋 **Alertas & Revisões:** <#{ch_ids['DISCORD_CHANNEL_ID']}>\n"
            f"• 📚 **Conteúdos:** <#{ch_ids['DISCORD_CONTENT_CHANNEL_ID']}>\n"
            f"• 📢 **Avisos da Turma:** <#{ch_ids['DISCORD_ANNOUNCEMENTS_CHANNEL_ID']}>\n"
            f"• ⚡ **Fila de Tarefas:** <#{ch_ids['DISCORD_QUEUE_CHANNEL_ID']}>\n"
            f"• 🎯 **Estudos & Simulados:** <#{ch_ids['DISCORD_STUDY_CHANNEL_ID']}>\n\n"
            "**Configuração rápida para o `.env` ou `configurar.bat`:**\n"
            f"```env\n"
            f"DISCORD_CHANNEL_ID={ch_ids['DISCORD_CHANNEL_ID']}\n"
            f"DISCORD_CONTENT_CHANNEL_ID={ch_ids['DISCORD_CONTENT_CHANNEL_ID']}\n"
            f"DISCORD_ANNOUNCEMENTS_CHANNEL_ID={ch_ids['DISCORD_ANNOUNCEMENTS_CHANNEL_ID']}\n"
            f"DISCORD_QUEUE_CHANNEL_ID={ch_ids['DISCORD_QUEUE_CHANNEL_ID']}\n"
            f"DISCORD_STUDY_CHANNEL_ID={ch_ids['DISCORD_STUDY_CHANNEL_ID']}\n"
            f"```"
        ),
        color=discord.Color.green()
    )
    embed.set_footer(text="Dica: Na interface gráfica (configurar.bat), basta clicar em 'Auto-Detectar Meus Canais'!")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ----------------------------------------------------
# 2. Comandos de Mensagem / Prefixo (!tarefas, !status, etc.)
# ----------------------------------------------------

@bot.command(name="tarefas")
async def prefix_tarefas(ctx: commands.Context, *, disciplina: Optional[str] = None):
    """Comando alternativo com prefixo: !tarefas [disciplina]."""
    embed = build_tarefas_embed(disciplina=disciplina)
    await ctx.send(embed=embed)


@bot.command(name="status")
async def prefix_status(ctx: commands.Context):
    """Comando alternativo com prefixo: !status."""
    embed = await build_status_embed()
    await ctx.send(embed=embed)


@bot.command(name="materiais")
async def prefix_materiais(ctx: commands.Context, *, disciplina: str):
    """Comando alternativo com prefixo: !materiais <disciplina>."""
    embed, err, discord_files = get_materiais_payload(disciplina)
    if err:
        await ctx.send(err)
    else:
        await ctx.send(embed=embed, files=discord_files)


@bot.command(name="resolver")
async def prefix_resolver(ctx: commands.Context, tarefa: str, *, instrucoes: Optional[str] = None):
    """Comando alternativo com prefixo: !resolver <id_ou_nome> [instruções]."""
    modo = "resolver"
    if instrucoes:
        if "--finalizar" in instrucoes or "--enviar" in instrucoes:
            modo = "finalizar"
            instrucoes = instrucoes.replace("--finalizar", "").replace("--enviar", "").strip() or None
        elif "--preencher" in instrucoes:
            modo = "preencher"
            instrucoes = instrucoes.replace("--preencher", "").strip() or None

    extra_files = []
    if ctx.message.attachments:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        for att in ctx.message.attachments:
            dest_file = temp_dir / sanitize_filename(att.filename)
            await att.save(dest_file)
            extra_files.append(dest_file)

    available_mats = get_course_materials_for_task(tarefa)
    if available_mats:
        embed = discord.Embed(
            title="📚 Seleção de Materiais de Apoio",
            description=(
                f"Foram identificados **{len(available_mats)} material(is)** salvos para esta disciplina.\n\n"
                "👉 **Selecione no menu abaixo até 3 arquivos** que a IA deve utilizar como referência:\n"
                "*(Ou clique diretamente em 'Resolver sem materiais extras')*"
            ),
            color=discord.Color.blue()
        )
        if extra_files:
            embed.add_field(
                name="📎 Arquivo Anexado por Você",
                value=f"`{extra_files[0].name}` (será enviado obrigatoriamente)",
                inline=False
            )
        view = MaterialSelectionView(
            tarefa=tarefa,
            instrucoes=instrucoes,
            attached_files=extra_files,
            available_materials=available_mats,
            send_func=ctx.send,
            interaction_or_ctx=ctx,
            is_refazer=False,
            modo=modo
        )
        await ctx.send(embed=embed, view=view)
        return

    await enqueue_solve_flow(
        send_func=ctx.send,
        tarefa=tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=False,
        modo=modo,
        requester=ctx.author.display_name,
        channel=ctx.channel
    )


@bot.command(name="resolver_lote", aliases=["lote"])
async def prefix_resolver_lote(ctx: commands.Context, *, args: Optional[str] = None):
    """Comando alternativo com prefixo: !resolver_lote [disciplina] [--instrucoes <texto>]."""
    extra_files = []
    if ctx.message.attachments:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        for att in ctx.message.attachments:
            dest_file = temp_dir / sanitize_filename(att.filename)
            await att.save(dest_file)
            extra_files.append(dest_file)

    disciplina = None
    instrucoes = None
    if args:
        if "--instrucoes" in args:
            parts = args.split("--instrucoes", 1)
            disciplina = parts[0].strip() or None
            instrucoes = parts[1].strip() or None
        else:
            disciplina = args.strip() or None

    assignments = await _get_current_assignments()

    pending_items = []
    norm_disc = normalize_text(disciplina) if disciplina else None

    for aid, item in assignments.items():
        if _is_task_completed(item):
            continue
        if norm_disc:
            item_course_norm = normalize_text(item.get("course", ""))
            if norm_disc not in item_course_norm:
                continue
        pending_items.append(item)

    def _sort_key(it):
        return (it.get("due_date", "9999"), natural_sort_key(it.get("title", "")))

    pending_items.sort(key=_sort_key)

    if not pending_items:
        msg = "🎉 Nenhuma atividade pendente encontrada"
        if disciplina:
            msg += f" para a matéria **{disciplina}**."
        else:
            msg += " no catálogo local do Moodle."
        await ctx.send(msg)
        return

    available_mats: List[Path] = []
    seen_mats = set()
    if disciplina:
        for p in get_course_materials_for_task(disciplina):
            if p.name not in seen_mats:
                seen_mats.add(p.name)
                available_mats.append(p)
    for it in pending_items[:25]:
        for p in get_course_materials_for_task(str(it.get("id", ""))):
            if p.name not in seen_mats:
                seen_mats.add(p.name)
                available_mats.append(p)

    view = BatchSelectView(
        pending_items=pending_items,
        disciplina_filter=disciplina,
        instrucoes=instrucoes,
        attached_files=extra_files,
        available_materials=available_mats,
        requester=ctx.author.display_name
    )
    embed = view.build_panel_embed()
    await ctx.send(embed=embed, view=view)


@bot.command(name="refazer")
async def prefix_refazer(ctx: commands.Context, tarefa: str, *, instrucoes: Optional[str] = None):
    """Comando alternativo com prefixo: !refazer <id_ou_nome> [instruções]."""
    extra_files = []
    if ctx.message.attachments:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        for att in ctx.message.attachments:
            dest_file = temp_dir / sanitize_filename(att.filename)
            await att.save(dest_file)
            extra_files.append(dest_file)

    available_mats = get_course_materials_for_task(tarefa)
    if available_mats:
        embed = discord.Embed(
            title="📚 Seleção de Materiais de Apoio (Refazer)",
            description=(
                f"Foram identificados **{len(available_mats)} material(is)** salvos para esta disciplina.\n\n"
                "👉 **Selecione no menu abaixo até 3 arquivos** que a IA deve utilizar como referência:\n"
                "*(Ou clique diretamente em 'Refazer sem materiais extras')*"
            ),
            color=discord.Color.blue()
        )
        if extra_files:
            embed.add_field(
                name="📎 Arquivo Anexado por Você",
                value=f"`{extra_files[0].name}` (será enviado obrigatoriamente)",
                inline=False
            )
        view = MaterialSelectionView(
            tarefa=tarefa,
            instrucoes=instrucoes,
            attached_files=extra_files,
            available_materials=available_mats,
            send_func=ctx.send,
            interaction_or_ctx=ctx,
            is_refazer=True
        )
        await ctx.send(embed=embed, view=view)
        return

    await enqueue_solve_flow(
        send_func=ctx.send,
        tarefa=tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=True,
        requester=ctx.author.display_name,
        channel=ctx.channel
    )


@bot.command(name="adicionarconteudo")
async def prefix_adicionarconteudo(ctx: commands.Context, *, disciplina: str):
    """Comando alternativo com prefixo: !adicionarconteudo <disciplina> (anexe arquivo)."""
    content_ch_id = settings.DISCORD_CONTENT_CHANNEL_ID
    ch_name = getattr(ctx.channel, "name", "")
    if content_ch_id and content_ch_id != 0 and ctx.channel.id != content_ch_id and ch_name != "conteudos":
        await ctx.send(f"⚠️ Este comando deve ser executado no canal dedicado a conteúdos: <#{content_ch_id}>.")
        return

    if not ctx.message.attachments:
        await ctx.send("⚠️ Por favor, anexe o arquivo (PDF, slide, resumo) junto com o comando `!adicionarconteudo <disciplina>`.")
        return

    dest_dir = resolve_course_materials_dir(disciplina)
    dest_dir.mkdir(parents=True, exist_ok=True)

    from src.scheduler.state import DaemonState
    state = DaemonState()
    from src.notifier.bridge_manager import cloud_bridge

    saved_names = []
    for att in ctx.message.attachments:
        dest_file = dest_dir / sanitize_filename(att.filename)
        await att.save(dest_file)
        saved_names.append(dest_file.name)

        state.register_custom_material(
            course=dest_dir.name,
            filename=dest_file.name,
            attachment_url=att.url,
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
            uploader=str(ctx.author),
            size=att.size
        )
        try:
            await cloud_bridge.register_material({
                "course": dest_dir.name,
                "filename": dest_file.name,
                "attachment_url": att.url,
                "channel_id": ctx.channel.id,
                "message_id": ctx.message.id,
                "uploader": str(ctx.author),
                "size": att.size
            })
        except Exception:
            pass

    embed = discord.Embed(
        title="📥 Conteúdo Adicionado à Base de Conhecimento!",
        description=f"Os arquivos **{', '.join(saved_names)}** foram salvos com sucesso.",
        color=discord.Color.green()
    )
    embed.add_field(name="🏫 Disciplina", value=dest_dir.name, inline=True)
    embed.set_footer(text="A IA passará a considerar este documento nas próximas resoluções.")
    await ctx.send(embed=embed)


@bot.command(name="perguntar")
async def prefix_perguntar(ctx: commands.Context, disciplina: str, *, duvida: str):
    """Tira dúvidas conceituais: !perguntar <disciplina> <dúvida>."""
    target_ch, redirected = get_study_target_channel(ctx)
    if redirected:
        await ctx.send(f"📍 Sua dúvida sobre **{disciplina}** foi encaminhada para o canal de estudos: <#{target_ch.id}>!")

    tutor = StudyTutor()
    res = await tutor.answer_question(discipline=disciplina, question=duvida)
    disc_clean = clean_display_course(disciplina)
    embed = discord.Embed(title=f"💡 Tutor Acadêmico: {disc_clean}", color=discord.Color.blue())
    embed.add_field(name="❓ Dúvida do Aluno", value=f"*{duvida[:500]}*", inline=False)
    ans_text = res.get("answer", "")
    embed.description = f"### 📖 Resposta do Tutor\n\n{ans_text[:4000]}"
    mats = res.get("materials_used", [])
    if mats:
        embed.add_field(name="📚 Materiais Consultados", value="\n".join(f"• `{m}`" for m in mats[:4]), inline=False)
    embed.set_footer(text=f"Solicitado por {ctx.author.display_name} • Modelo: {res.get('model_used')}")
    await target_ch.send(embed=embed)


@bot.command(name="flashcards")
async def prefix_flashcards(ctx: commands.Context, disciplina: str, *, topico: Optional[str] = None):
    """Gera flashcards e deck Anki: !flashcards <disciplina> [tópico]."""
    target_ch, redirected = get_study_target_channel(ctx)
    if redirected:
        await ctx.send(f"📍 Seus flashcards para **{disciplina}** estão no canal de estudos: <#{target_ch.id}>!")

    tutor = StudyTutor()
    res = await tutor.generate_flashcards(discipline=disciplina, topic=topico, count=8)
    view = FlashcardsCarouselView(
        cards=res.get("cards", []),
        discipline=disciplina,
        topic=res.get("topic", "Geral"),
        requester=ctx.author.display_name
    )
    embed = view.build_embed()
    anki_path = res.get("anki_file_path")
    file_to_send = discord.File(str(anki_path), filename=Path(anki_path).name) if anki_path and Path(anki_path).exists() else None
    if file_to_send:
        await target_ch.send(embed=embed, view=view, file=file_to_send)
    else:
        await target_ch.send(embed=embed, view=view)


@bot.command(name="quiz")
async def prefix_quiz(ctx: commands.Context, disciplina: str, qtd: Optional[int] = 5):
    """Gera simulado pré-prova: !quiz <disciplina> [qtd_questoes]."""
    target_ch, redirected = get_study_target_channel(ctx)
    if redirected:
        await ctx.send(f"📍 Seu simulado para **{disciplina}** foi iniciado no canal de estudos: <#{target_ch.id}>!")

    tutor = StudyTutor()
    num_q = max(3, min(qtd or 5, 10))
    res = await tutor.generate_quiz(discipline=disciplina, num_questions=num_q)
    view = InteractiveQuizSessionView(
        questions=res.get("questions", []),
        discipline=disciplina,
        topic=res.get("topic", "Geral"),
        requester=ctx.author.display_name,
        requester_id=ctx.author.id
    )
    embed = view.build_question_embed()
    await target_ch.send(embed=embed, view=view)


@bot.command(name="meuscanais")
async def prefix_meuscanais(ctx: commands.Context):
    """Cria ou localiza suas salas privadas no servidor: !meuscanais."""
    if not ctx.guild:
        await ctx.send("❌ Este comando deve ser executado dentro de um servidor do Discord.")
        return

    processing_msg = await ctx.send("⏳ Localizando/criando suas salas privadas...")
    try:
        res = await asyncio.wait_for(
            provision_user_channels(ctx.guild, ctx.author),
            timeout=20.0
        )
    except asyncio.TimeoutError:
        await processing_msg.edit(content=
            "⏱️ **Tempo esgotado.** O Discord demorou para responder — tente novamente.\n"
            "Se persistir, verifique se o bot tem permissão **Gerenciar Canais** no servidor."
        )
        return
    except discord.Forbidden:
        await processing_msg.edit(content=
            "🚫 **Sem permissão.** O bot precisa da permissão **Gerenciar Canais** no servidor.\n"
            "Peça ao administrador para conceder essa permissão ao bot."
        )
        return

    ch_ids = res["channels"]
    embed = discord.Embed(
        title="🔒 Suas Salas Pessoais do Moodle Bot",
        description=(
            f"Categoria: **{res['category_name']}**\n\n"
            f"• 📋 Alertas: <#{ch_ids['DISCORD_CHANNEL_ID']}>\n"
            f"• 📚 Conteúdos: <#{ch_ids['DISCORD_CONTENT_CHANNEL_ID']}>\n"
            f"• 📢 Avisos: <#{ch_ids['DISCORD_ANNOUNCEMENTS_CHANNEL_ID']}>\n"
            f"• ⚡ Fila: <#{ch_ids['DISCORD_QUEUE_CHANNEL_ID']}>\n"
            f"• 🎯 Estudos: <#{ch_ids['DISCORD_STUDY_CHANNEL_ID']}>\n"
        ),
        color=discord.Color.green()
    )
    await processing_msg.delete()
    await ctx.send(embed=embed)


@bot.command(name="ajuda")
async def prefix_ajuda(ctx: commands.Context):
    """Exibe o guia de comandos do robô."""
    embed = discord.Embed(
        title="🤖 Moodle AI Assistant - Comandos Disponíveis",
        description="Você pode interagir usando comandos de barra (`/`) ou prefixo (`!`):",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔒 `!meuscanais` ou `/meuscanais`", value="Cria ou localiza suas 5 salas privadas exclusivas neste servidor.", inline=False)
    embed.add_field(name="📋 `!tarefas` ou `/tarefas [disciplina]`", value="Lista tarefas e questionários pendentes e concluídos.", inline=False)
    embed.add_field(name="📖 `!materiais <disciplina>` ou `/materiais`", value="Envia slides e materiais de estudo no chat.", inline=False)
    embed.add_field(name="🧠 `!resolver <id_ou_nome>` ou `/resolver`", value="Resolve atividade ou questionário sob demanda com IA.", inline=False)
    embed.add_field(name="📦 `!resolver_lote` ou `/resolver_lote`", value="Menu interativo para selecionar e resolver múltiplas tarefas em lote.", inline=False)
    embed.add_field(name="🔄 `!refazer <id_ou_nome>` ou `/refazer`", value="Refaz atividade ou questionário já concluído com IA.", inline=False)
    embed.add_field(name="💡 `!perguntar <disciplina> <dúvida>` ou `/perguntar`", value="Tutor acadêmico que tira dúvidas citando slides do professor.", inline=False)
    embed.add_field(name="🗂️ `!flashcards <disciplina> [tópico]` ou `/flashcards`", value="Gera baralho de flashcards com exportação direta para o Anki.", inline=False)
    embed.add_field(name="📝 `!quiz <disciplina> [qtd]` ou `/quiz`", value="Simulado interativo pré-prova com pegadinhas e botões A, B, C, D.", inline=False)
    embed.add_field(name="🛰️ `!status` ou `/status`", value="Exibe a sessão do Moodle, materiais e IA.", inline=False)
    embed.add_field(name="📥 `!adicionarconteudo <disciplina>` (com anexo)", value="Salva resumos e materiais na memória da IA.", inline=False)
    await ctx.send(embed=embed)


class MoodleDiscordNotifier:
    """Cliente unificado do Discord para notificações de rascunhos, alertas e notas."""

    def __init__(self, token: Optional[str] = None, channel_id: Optional[int] = None):
        self.token = token or settings.DISCORD_BOT_TOKEN
        self.channel_id = channel_id or settings.DISCORD_CHANNEL_ID

    async def _resolve_channel(self, channel_id: Optional[Any] = None) -> Optional[discord.abc.Messageable]:
        """Obtém o canal do Discord com fallback seguro para fetch_channel mesmo em background/runner."""
        raw_id = channel_id or self.channel_id or settings.DISCORD_CHANNEL_ID
        if not raw_id:
            return None
        try:
            target_id = int(raw_id)
        except (ValueError, TypeError):
            return None

        if bot.is_ready():
            channel = bot.get_channel(target_id)
            if channel:
                return channel

        token = self.token or settings.DISCORD_BOT_TOKEN
        if token and getattr(bot.http, "token", None) != token:
            try:
                await bot.login(token)
            except Exception:
                pass

        if getattr(bot._connection, "_ready", None) is discord.utils.MISSING:
            bot._connection._ready = asyncio.Event()
            bot._connection._ready.set()

        try:
            return await asyncio.wait_for(bot.fetch_channel(target_id), timeout=6.0)
        except Exception:
            return None

    async def send_assignment_review(
        self,
        assignment: Assignment,
        draft: SolutionDraft,
        draft_saved: bool = False,
        is_finalized: bool = False,
        final_status_message: Optional[str] = None,
        channel: Optional[Any] = None,
    ) -> bool:
        """Envia o rascunho de resolução (PDF e Markdown) para o canal privado com botões."""
        if not self.token or self.token == "seu_discord_bot_token_aqui":
            console.print("[yellow]Aviso: DISCORD_BOT_TOKEN não configurado no .env.[/yellow]")
            return False

        if not channel and (not self.channel_id or self.channel_id == 0):
            console.print("[yellow]Aviso: DISCORD_CHANNEL_ID não configurado no .env.[/yellow]")
            return False

        sent_success = False
        # Prioriza DOCX (manual) sobre PDF (automático), fallback para markdown
        file_to_send = (
            draft.docx_path if (draft.docx_path and draft.docx_path.exists())
            else draft.pdf_path if (draft.pdf_path and draft.pdf_path.exists())
            else draft.output_path
        )

        act_type = getattr(assignment, "activity_type", "assign")
        type_str = "Questionário Online" if act_type == "quiz" else "Trabalho Acadêmico"

        try:
            target_ch = channel or (await self._resolve_channel())

            if target_ch:
                if is_finalized:
                    embed_desc = (
                        "⚡ **Atividade resolvida e enviada em definitivo no Moodle (End-to-End)!**\n\n"
                        f"{final_status_message or 'Envio concluído com sucesso.'}\n\n"
                        "Você pode abrir o Moodle a qualquer momento para verificar o comprovante."
                    )
                    embed_color = discord.Color.green()
                    embed_title = f"✅ Submetido com Sucesso: {assignment.title}"
                    footer_text = f"Finalizado no Moodle em modo autônomo às {datetime.now().strftime('%H:%M:%S')}"
                    msg_header = f"⚡ **Atividade resolvida e enviada com sucesso no Moodle:** `{assignment.title}`"
                elif draft_saved:
                    embed_desc = (
                        "📝 **Respostas resolvidas pela IA e preenchidas no Moodle!**\n\n"
                        f"{final_status_message or 'As respostas foram salvas na tentativa sem submeter.'}\n\n"
                        "👉 Verifique no Moodle ou clique no botão **[🚀 Enviar Tudo e Terminar]** abaixo quando desejar finalizar."
                    )
                    embed_color = discord.Color.blue()
                    embed_title = f"📝 Rascunho Salvo: {assignment.title}"
                    footer_text = f"Respostas salvas na tentativa do Moodle às {datetime.now().strftime('%H:%M:%S')}. Aguardando envio definitivo."
                    msg_header = f"📝 **Respostas resolvidas e salvas na tentativa do Moodle:** `{assignment.title}`"
                elif act_type == "quiz":
                    embed_desc = (
                        "As respostas para o questionário online foram preparadas pela IA.\n\n"
                        "• **[📝 Apenas Preencher Quiz]**: Digita as respostas no Moodle e salva na tentativa sem submeter. Você poderá abrir o Moodle e conferir!\n"
                        "• **[🚀 Enviar Tudo e Terminar]**: Finaliza a tentativa e confirma o envio no Moodle."
                    )
                    embed_color = discord.Color.blue()
                    embed_title = f"📋 Revisão: {assignment.title}"
                    footer_text = "Ação humana obrigatória • Clique abaixo para submeter"
                    msg_header = f"🔔 **Nova resolução pronta para revisão:** `{assignment.title}`"
                else:
                    embed_desc = (
                        f"As respostas foram preparadas para sua conferência ({type_str}).\n"
                        "Leia o documento anexado abaixo e confirme o envio usando os botões."
                    )
                    embed_color = discord.Color.blue()
                    embed_title = f"📋 Revisão: {assignment.title}"
                    footer_text = "Ação humana obrigatória • Clique abaixo para submeter"
                    msg_header = f"🔔 **Nova resolução pronta para revisão:** `{assignment.title}`"

                embed = discord.Embed(
                    title=embed_title,
                    url=assignment.url,
                    description=embed_desc,
                    color=embed_color
                )

                embed.add_field(name="🏫 Disciplina", value=assignment.course_name, inline=False)
                embed.add_field(name="⏰ Prazo de Entrega", value=assignment.due_date_str or "Não especificado", inline=True)
                embed.add_field(name="⏳ Tempo Restante", value=assignment.time_remaining or "N/A", inline=True)
                embed.add_field(name="🧠 Modelo Utilizado", value=f"`{draft.used_model}`", inline=True)

                summary_raw = (draft.summary or "").strip()
                if not summary_raw and draft.structured_answers:
                    items = []
                    if isinstance(draft.structured_answers, list):
                        for it in draft.structured_answers[:10]:
                            k = it.get("key") or it.get("field") or "Q"
                            v = it.get("value", "")
                            items.append(f"• {k}: {v}")
                    elif isinstance(draft.structured_answers, dict):
                        for k, v in list(draft.structured_answers.items())[:10]:
                            items.append(f"• {k}: {v}")
                    summary_raw = "\n".join(items)

                if not summary_raw:
                    summary_raw = "Respostas resolvidas pela IA e salvas na tentativa do Moodle."

                summary_text = summary_raw[:800] + ("..." if len(summary_raw) > 800 else "")
                embed.add_field(name="📝 Respostas Preparadas", value=f"```markdown\n{summary_text}\n```", inline=False)

                if draft.used_materials:
                    embed.add_field(
                        name="📚 Fontes & Materiais de Referência",
                        value="\n".join(f"• `{m}`" for m in draft.used_materials[:5]),
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="📚 Fontes & Materiais de Referência",
                        value="ℹ️ Nenhum arquivo externo utilizado. A resolução foi baseada exclusivamente no enunciado e questões extraídos diretamente do Moodle.",
                        inline=False
                    )

                if assignment.url:
                    embed.add_field(
                        name="🔗 Acesso Direto",
                        value=f"[Abrir Atividade no Moodle UFMG]({assignment.url})",
                        inline=False
                    )

                embed.set_footer(text=footer_text)

                discord_file = discord.File(str(file_to_send), filename=file_to_send.name)
                view = ReviewActionView(
                    assignment_id=assignment.id,
                    assignment_url=assignment.url,
                    file_to_submit=file_to_send,
                    activity_type=act_type,
                    structured_answers=draft.structured_answers,
                    draft_saved=draft_saved,
                    is_finalized=is_finalized,
                    draft=draft,  # para o botão "✏️ Fazer Modificação"
                )

                await target_ch.send(
                    content=msg_header,
                    embed=embed,
                    file=discord_file,
                    view=view
                )
                console.print(f"[green]✔ Card de revisão com anexo {file_to_send.name} enviado no Discord![/green]")
                return True

        except Exception as e:
            console.print(f"[red]Erro ao enviar mensagem no Discord via bot global: {e}[/red]")

        return sent_success

    async def send_grade_notification(
        self,
        course_name: str,
        assignment_title: str,
        grade: str,
        feedback: Optional[str] = None,
        graded_by: Optional[str] = None
    ) -> bool:
        """Envia aviso festivo quando o professor publica a nota e correção no Moodle."""
        try:
            channel = await self._resolve_channel()
            if channel:
                embed = discord.Embed(
                    title=f"🎉 Nota Publicada: {assignment_title}",
                    description=f"O professor avaliou sua atividade na disciplina **{course_name}**!",
                    color=discord.Color.green()
                )
                embed.add_field(name="📊 Nota Atribuída", value=f"**{grade}**", inline=True)
                if graded_by:
                    embed.add_field(name="👨‍🏫 Avaliador", value=graded_by, inline=True)
                if feedback:
                    embed.add_field(name="💬 Feedback / Comentários do Professor", value=f"```\n{feedback}\n```", inline=False)

                embed.set_footer(text=f"UFMG Virtual • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
                await channel.send(content="🔔 **Resultado de Avaliação Disponível!**", embed=embed)
                return True
        except Exception as e:
            console.print(f"[red]Erro ao enviar notificação de nota: {e}[/red]")
        return False

    async def send_course_announcement(
        self,
        announcement
    ) -> bool:
        """Envia comunicado/aviso da turma publicado pelo professor para o Discord."""
        try:
            target_ch_id = settings.DISCORD_ANNOUNCEMENTS_CHANNEL_ID or self.channel_id
            if not target_ch_id or target_ch_id == 0:
                target_ch_id = self.channel_id

            if not bot.is_ready():
                if self.token:
                    try:
                        await asyncio.wait_for(bot.wait_until_ready(), timeout=5.0)
                    except Exception:
                        pass

            channel = bot.get_channel(target_ch_id)
            if not channel and bot.is_ready():
                try:
                    channel = await bot.fetch_channel(target_ch_id)
                except Exception:
                    pass

            if not channel:
                channel = await self._resolve_channel()

            if channel:
                embed = discord.Embed(
                    title=f"📢 Novo Aviso: {announcement.title}",
                    url=announcement.url,
                    color=discord.Color.gold()
                )
                embed.add_field(name="🏫 Disciplina", value=announcement.course_name, inline=False)
                if announcement.author:
                    embed.add_field(name="👤 Publicado por", value=announcement.author, inline=True)
                if announcement.date:
                    embed.add_field(name="📅 Data / Hora", value=announcement.date, inline=True)

                msg_content = announcement.message.strip() if announcement.message else "Clique no link abaixo para ler o comunicado completo no Moodle."
                if len(msg_content) > 1800:
                    msg_content = msg_content[:1800] + "\n\n*(Mensagem longa truncada - abra no Moodle para ler na íntegra)*"

                embed.description = msg_content
                embed.set_footer(text=f"Moodle UFMG • {datetime.now().strftime('%d/%m/%Y %H:%M')}")

                view = ui.View(timeout=None)
                view.add_item(ui.Button(
                    label="Abrir Aviso no Moodle",
                    style=discord.ButtonStyle.link,
                    url=announcement.url,
                    emoji="🔗"
                ))

                await channel.send(
                    content=f"📣 **Aviso da Turma:** Novo comunicado em **{announcement.course_name}**!",
                    embed=embed,
                    view=view
                )
                return True
        except Exception as e:
            console.print(f"[red]Erro ao enviar comunicado da turma: {e}[/red]")
        return False

    async def send_notion_announcement(
        self,
        title: str,
        item_type: str = "Atividade / Tarefa",
        course_name: Optional[str] = None,
        date_str: Optional[str] = None,
        notion_url: Optional[str] = None,
        moodle_url: Optional[str] = None,
        details: Optional[str] = None,
        steps: Optional[List[str]] = None,
        notes: Optional[str] = None,
        status: Optional[str] = "não iniciado",
        action: str = "Novo Item Registrado no Notion"
    ) -> bool:
        """Envia anúncio detalhado no canal de avisos do Discord sempre que algo for adicionado ao Notion."""
        try:
            target_ch_id = settings.DISCORD_ANNOUNCEMENTS_CHANNEL_ID or self.channel_id
            if not target_ch_id or target_ch_id == 0:
                target_ch_id = self.channel_id

            if not bot.is_ready():
                if self.token:
                    try:
                        await asyncio.wait_for(bot.wait_until_ready(), timeout=5.0)
                    except Exception:
                        pass

            channel = bot.get_channel(target_ch_id)
            if not channel and bot.is_ready():
                try:
                    channel = await bot.fetch_channel(target_ch_id)
                except Exception:
                    pass

            if not channel:
                channel = await self._resolve_channel()

            color = discord.Color.from_rgb(15, 122, 116)  # Notion teal
            embed = discord.Embed(
                title=f"📓 {action}: {title}",
                url=notion_url if notion_url else None,
                description="Um novo item foi catalogado e organizado na sua **Central de Estudos no Notion**.",
                color=color,
                timestamp=datetime.now()
            )

            embed.add_field(name="📌 Categoria / Tipo", value=f"`{item_type}`", inline=True)

            if course_name:
                embed.add_field(name="🏫 Disciplina", value=f"**{course_name}**", inline=True)

            if date_str:
                embed.add_field(name="📅 Data / Prazo", value=f"📆 **{date_str}**", inline=True)

            if status:
                status_display = status.capitalize()
                status_emoji = "🟢" if "conclu" in status.lower() else ("🟡" if "andamento" in status.lower() else "⚪")
                embed.add_field(name="📊 Status no Notion", value=f"{status_emoji} {status_display}", inline=True)

            if details:
                clean_details = details.strip()
                if len(clean_details) > 1000:
                    clean_details = clean_details[:997] + "..."
                embed.add_field(name="📝 Detalhes e Conteúdo", value=clean_details, inline=False)

            if steps:
                steps_text = "\n".join([f"• {s}" for s in steps])
                if len(steps_text) > 800:
                    steps_text = steps_text[:797] + "..."
                embed.add_field(name="📋 Etapas de Estudo Planejadas", value=steps_text, inline=False)

            if notes:
                embed.add_field(name="💡 Observações", value=notes[:500], inline=False)

            embed.set_footer(text="Notion Assistant • Sincronizado automaticamente")

            buttons = []
            if notion_url:
                buttons.append(ui.Button(
                    label="Abrir no Notion",
                    style=discord.ButtonStyle.link,
                    url=notion_url,
                    emoji="📓"
                ))
            if moodle_url:
                buttons.append(ui.Button(
                    label="Abrir no Moodle",
                    style=discord.ButtonStyle.link,
                    url=moodle_url,
                    emoji="🔗"
                ))

            view = None
            if buttons:
                view = ui.View(timeout=None)
                for btn in buttons:
                    view.add_item(btn)

            if channel:
                await channel.send(
                    content=f"📢 **Notion Atualizado:** O bot acabou de adicionar `{title}` na sua central de estudos!",
                    embed=embed,
                    view=view
                )
                console.print(f"[bold green]✔ Anúncio detalhado enviado ao canal do Discord para o item do Notion: {title}[/bold green]")
                return True
            elif self.token and target_ch_id:
                # Fallback direto via REST API do Discord caso o gateway não esteja conectado
                import httpx
                rest_url = f"https://discord.com/api/v10/channels/{target_ch_id}/messages"
                rest_headers = {
                    "Authorization": f"Bot {self.token}",
                    "Content-Type": "application/json"
                }
                rest_body = {
                    "content": f"📢 **Notion Atualizado:** O bot acabou de adicionar `{title}` na sua central de estudos!",
                    "embeds": [embed.to_dict()]
                }
                async with httpx.AsyncClient(timeout=10.0) as http_client:
                    r = await http_client.post(rest_url, headers=rest_headers, json=rest_body)
                    if r.status_code in [200, 201]:
                        console.print(f"[bold green]✔ Anúncio detalhado enviado via REST ao canal do Discord para: {title}[/bold green]")
                        return True
                    else:
                        console.print(f"[yellow]Aviso Discord REST {r.status_code}: {r.text}[/yellow]")
            return False
        except Exception as e:
            console.print(f"[red]Erro ao enviar anúncio do Notion no Discord: {e}[/red]")
        return False

    async def send_daily_checklist_announcement(
        self,
        day_name: str,
        date_str: str,
        checklist_items: List[str],
        notion_url: Optional[str] = None
    ) -> bool:
        """Envia um briefing matinal com as tarefas do dia sincronizadas no Notion."""
        try:
            target_ch_id = settings.DISCORD_ANNOUNCEMENTS_CHANNEL_ID or settings.DISCORD_CHANNEL_ID
            channel = None
            if bot.is_ready():
                channel = bot.get_channel(target_ch_id)
                if not channel:
                    try:
                        channel = await bot.fetch_channel(target_ch_id)
                    except Exception:
                        pass
            if not channel:
                channel = await self._resolve_channel()

            color = discord.Color.from_rgb(34, 139, 34)  # Forest Green / Notion
            embed = discord.Embed(
                title=f"🌅 Tarefas do Dia: {day_name} ({date_str})",
                url=notion_url if notion_url else None,
                description=(
                    "Sua checklist de **tarefas do dia** no Notion foi atualizada automaticamente com "
                    "seus compromissos da rotina semanal e prazos acadêmicos!"
                ),
                color=color,
                timestamp=datetime.now()
            )

            # Formata os itens para o Discord
            task_lines = []
            for item in checklist_items:
                task_lines.append(f"☐ {item}")

            tasks_text = "\n".join(task_lines)
            if len(tasks_text) > 1900:
                tasks_text = tasks_text[:1890] + "\n... (mais itens no Notion)"

            embed.add_field(
                name=f"📋 Checklist Diária ({len(checklist_items)} tarefas)",
                value=tasks_text or "Nenhuma tarefa para hoje!",
                inline=False
            )
            embed.set_footer(text="Central de Estudos • Notion Assistant")

            view = None
            if notion_url:
                view = ui.View(timeout=None)
                view.add_item(ui.Button(
                    label="Abrir no Notion",
                    style=discord.ButtonStyle.link,
                    url=notion_url,
                    emoji="📓"
                ))

            if channel:
                await channel.send(
                    content=f"🌅 **Bom dia!** Suas tarefas de hoje ({day_name}) estão prontas no Notion:",
                    embed=embed,
                    view=view
                )
                console.print(f"[bold green]✔ Briefing da checklist diária enviado ao Discord para {day_name}[/bold green]")
                return True
            elif self.token and target_ch_id:
                # Fallback via REST API
                import httpx
                rest_url = f"https://discord.com/api/v10/channels/{target_ch_id}/messages"
                rest_headers = {
                    "Authorization": f"Bot {self.token}",
                    "Content-Type": "application/json"
                }
                rest_body = {
                    "content": f"🌅 **Bom dia!** Suas tarefas de hoje ({day_name}) estão prontas no Notion:",
                    "embeds": [embed.to_dict()]
                }
                async with httpx.AsyncClient(timeout=10.0) as http_client:
                    r = await http_client.post(rest_url, headers=rest_headers, json=rest_body)
                    if r.status_code in [200, 201]:
                        console.print(f"[bold green]✔ Briefing da checklist enviado via REST ao Discord para {day_name}[/bold green]")
                        return True
                    else:
                        console.print(f"[yellow]Aviso Discord REST {r.status_code}: {r.text}[/yellow]")
            return False
        except Exception as e:
            console.print(f"[red]Erro ao enviar anúncio de checklist diária no Discord: {e}[/red]")
        return False

    async def send_countdown_alert(
        self,
        title: str,
        course_name: str,
        minutes_remaining: int
    ) -> bool:
        """Envia alertas intensivos de contagem regressiva para prazos próximos."""
        try:
            channel = await self._resolve_channel()
            if channel:
                if minutes_remaining <= 1:
                    content = "@everyone 🚨🚨 **ÚLTIMO MINUTO! PRAZO ENCERRANDO!** 🚨🚨"
                    color = discord.Color.dark_red()
                    title_text = f"🔥 MENOS DE 60 SEGUNDOS: {title}"
                    desc = (
                        f"O prazo da atividade **{title}** ({course_name}) encerra em **menos de 1 minuto**!\n"
                        f"Abra o card acima e clique em **[✅ Aprovar e Enviar]** IMEDIATAMENTE ou envie você mesmo no Moodle!"
                    )
                elif minutes_remaining <= 5:
                    content = "@everyone ⏰ **ALERTA DE URGÊNCIA (5 MINUTOS)!**"
                    color = discord.Color.red()
                    title_text = f"🚨 FALTAM 5 MINUTOS: {title}"
                    desc = (
                        f"Faltam apenas **5 minutos** para o vencimento de **{title}** ({course_name})!\n"
                        f"Confira o PDF anexado acima e aprove o envio se desejar entregar."
                    )
                else:
                    content = f"⚠️ **Atenção:** Prazo de entrega se aproximando ({minutes_remaining} min)!"
                    color = discord.Color.orange()
                    title_text = f"⚠️ Faltam {minutes_remaining} minutos: {title}"
                    desc = f"A atividade **{title}** ({course_name}) encerra em {minutes_remaining} minutos."

                embed = discord.Embed(title=title_text, description=desc, color=color)
                await channel.send(content=content, embed=embed)
                return True
        except Exception as e:
            console.print(f"[red]Erro ao enviar alerta de contagem: {e}[/red]")
        return False

    async def send_session_expired_alert(self, browser_opened: bool = True) -> bool:
        """Envia alerta no canal de alertas informando que a sessão do Moodle expirou e abrindo o login."""
        try:
            channel = await self._resolve_channel(self.channel_id)
            if not channel:
                return False

            desc_lines = [
                "A sua sessão de autenticação no **Moodle UFMG / MinhaUFMG** expirou no servidor.\n"
            ]
            if browser_opened:
                desc_lines.append(
                    "🖥️ **A janela do navegador já foi aberta automaticamente no seu computador!**\n"
                    "Basta preencher seu **usuário e senha** na tela do MinhaUFMG. Assim que o Moodle carregar, a nova sessão será salva e o bot voltará a operar normalmente.\n"
                )
            desc_lines.append(
                "👉 **Caso a janela tenha sido fechada ou precise reabri-la:**\n"
                "• Clique no botão **'🔑 Abrir Login no PC'** abaixo;\n"
                "• Ou execute no terminal: `.venv\\Scripts\\python.exe -m src.auth.moodle_auth`.\n\n"
                "*(O assistente continuará monitorando prazos, mas não conseguirá acessar questões internas nem enviar respostas até a renovação.)*"
            )

            embed = discord.Embed(
                title="⚠️ Sessão do Moodle Expirada",
                description="\n".join(desc_lines),
                color=discord.Color.red()
            )
            embed.set_footer(text=f"Detectado pelo Heartbeat do Moodle Bot às {datetime.now().strftime('%H:%M:%S')}")
            view = SessionExpiredView()
            content = (
                "⚠️ **Atenção:** Sua sessão de login no Moodle expirou! A tela de login foi aberta no seu computador."
                if browser_opened
                else "⚠️ **Atenção:** Sua sessão de login no Moodle expirou!"
            )
            await channel.send(
                content=content,
                embed=embed,
                view=view
            )
            return True
        except Exception as e:
            console.print(f"[yellow]Nota ao enviar alerta de sessão expirada no Discord: {e}[/yellow]")
            return False

    async def send_session_renewed_notification(self, user_name: Optional[str] = None) -> bool:
        """Envia mensagem no Discord confirmando que a sessão do Moodle foi restabelecida com sucesso."""
        try:
            channel = await self._resolve_channel(self.channel_id)
            if not channel:
                return False

            embed = discord.Embed(
                title="🎉 Sessão do Moodle Renovada com Sucesso!",
                description=(
                    f"A sua autenticação no **Moodle UFMG / MinhaUFMG** foi restabelecida com êxito"
                    f"{' para **' + user_name + '**' if user_name else ''}.\n\n"
                    "✔ Acesso a disciplinas, questionários e materiais desbloqueado.\n"
                    "✔ O assistente continuará monitorando e resolvendo tarefas em segundo plano."
                ),
                color=discord.Color.green()
            )
            embed.set_footer(text=f"Renovado às {datetime.now().strftime('%H:%M:%S')}")
            await channel.send(
                content="✅ **Sessão do Moodle Reativada!**",
                embed=embed
            )
            return True
        except Exception as e:
            console.print(f"[yellow]Nota ao enviar notificação de sessão renovada no Discord: {e}[/yellow]")
            return False


async def run_bot():
    """Inicia o Bot do Discord com os Slash Commands ativos."""
    if not settings.DISCORD_BOT_TOKEN:
        console.print("[red]Erro: DISCORD_BOT_TOKEN não configurado.[/red]")
        return
    await bot.start(settings.DISCORD_BOT_TOKEN)


def main():
    parser = argparse.ArgumentParser(description="Moodle Discord Bot (Slash Commands)")
    parser.add_argument("--run", action="store_true", help="Inicia o bot e registra os Slash Commands")
    args = parser.parse_args()

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
