"""Adaptador Canvas LMS (CanvasAdapter) para o LumiBot.

Implementa BaseLMSProvider integrando diretamente com a API REST v1 do Canvas
(Instructure) usando httpx assíncrono, suporte a paginação via cabeçalho Link,
Rate Limiting e tratamento de erros 401.
"""

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from rich.console import Console

from config.settings import settings
from src.core.models import LMSCourse, LMSAssignment, LMSAnnouncement
from src.providers.base import BaseLMSProvider

console = Console()


# =====================================================================
# Exceções customizadas da integração com o Canvas LMS
# =====================================================================

class CanvasError(Exception):
    """Exceção base para erros relacionados ao Canvas LMS."""
    pass


class CanvasAuthenticationError(CanvasError):
    """Erro de autenticação 401 (Token Bearer ausente, inválido ou expirado)."""
    pass


class CanvasRateLimitError(CanvasError):
    """Erro de limite de taxa 429 (Rate Limit Exceeded no Canvas)."""
    pass


class CanvasAPIError(CanvasError):
    """Erro retornado pela API REST do Canvas (4xx / 5xx)."""

    def __init__(self, status_code: int, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(f"Erro Canvas API [{status_code}]: {message}")
        self.status_code = status_code
        self.message = message
        self.details = details or {}


# =====================================================================
# Funções utilitárias de parsing e headers
# =====================================================================

def parse_link_header(link_header: str) -> Dict[str, str]:
    """Extrai as URLs de paginação do cabeçalho Link (RFC 5988 / RFC 9707).

    Exemplo:
        <https://.../api/v1/courses?page=2>; rel="next", <https://...>; rel="first"
    Retorna:
        {"next": "https://.../api/v1/courses?page=2", "first": "https://..."}
    """
    links: Dict[str, str] = {}
    if not link_header:
        return links

    # Separa por vírgulas que dividem cada link
    for part in link_header.split(","):
        sections = [s.strip() for s in part.split(";")]
        if len(sections) < 2:
            continue
        url = sections[0].strip("<> ")
        for param in sections[1:]:
            match = re.search(r'rel=["\']?([^"\';\s]+)["\']?', param)
            if match:
                rel = match.group(1).lower()
                links[rel] = url
    return links


def parse_canvas_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Converte strings ISO 8601 da API do Canvas em objetos datetime com timezone."""
    if not dt_str:
        return None
    try:
        # Suporta strings com final 'Z' ou offset '+00:00'
        clean = dt_str.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        return datetime.fromisoformat(clean)
    except Exception:
        return None


# =====================================================================
# Adaptador Canvas LMS
# =====================================================================

class CanvasAdapter(BaseLMSProvider):
    """Adaptador de integração para o Canvas LMS (Instructure)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_token: Optional[str] = None,
        timeout: float = 30.0
    ):
        raw_url = base_url or getattr(settings, "CANVAS_BASE_URL", "https://pucminas.instructure.com")
        self.base_url = raw_url.rstrip("/")
        self.api_token = (
            api_token
            or os.environ.get("CANVAS_API_TOKEN")
            or getattr(settings, "CANVAS_API_TOKEN", "")
        ).strip()
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def platform(self) -> str:
        return "canvas"

    def _get_client(self) -> httpx.AsyncClient:
        """Cria ou reaproveita o cliente HTTP assíncrono com cabeçalhos padrão."""
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/json",
                "User-Agent": "LumiBot-CanvasAdapter/2.0",
            }
            if self.api_token:
                headers["Authorization"] = f"Bearer {self.api_token}"

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Fecha a sessão do cliente HTTP assíncrono."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # -----------------------------------------------------------------
    # Camada de Comunicação HTTP com Resiliência e Rate Limit
    # -----------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        retries: int = 2
    ) -> httpx.Response:
        """Executa uma requisição HTTP com tratamento de autenticação e rate limiting."""
        client = self._get_client()

        # Garante que paths comecem com barra
        if not path.startswith("http"):
            url = path if path.startswith("/") else f"/{path}"
        else:
            url = path

        attempt = 0
        while attempt <= retries:
            attempt += 1
            try:
                response = await client.request(method, url, params=params)

                # Inspeção de Rate Limit (Canvas envia X-Rate-Limit-Remaining)
                rate_remaining = response.headers.get("X-Rate-Limit-Remaining")
                if rate_remaining is not None:
                    try:
                        rem_val = float(rate_remaining)
                        if rem_val < 50.0:
                            console.print(f"[yellow]⚠️ Canvas Rate Limit baixo: {rem_val} restantes[/yellow]")
                    except ValueError:
                        pass

                # Tratamento de 401 Unauthorized
                if response.status_code == 401:
                    raise CanvasAuthenticationError(
                        "Autenticação no Canvas falhou (401 Unauthorized). "
                        "Verifique se a variável CANVAS_API_TOKEN no arquivo .env é válida."
                    )

                # Tratamento de 429 Too Many Requests (Rate Limit Exceeded)
                if response.status_code == 429:
                    if attempt <= retries:
                        wait_seconds = 2.0 * attempt
                        console.print(f"[yellow]⏳ Canvas Rate Limit atingido (429). Aguardando {wait_seconds}s...[/yellow]")
                        await asyncio.sleep(wait_seconds)
                        continue
                    raise CanvasRateLimitError("Limite de requisições excedido no Canvas LMS (429 Rate Limit).")

                # Demais códigos de erro HTTP
                if response.is_error:
                    error_msg = response.text
                    try:
                        err_json = response.json()
                        error_msg = err_json.get("message") or err_json.get("errors") or response.text
                    except Exception:
                        pass
                    raise CanvasAPIError(response.status_code, str(error_msg))

                return response

            except httpx.RequestError as exc:
                if attempt <= retries:
                    await asyncio.sleep(1.5 * attempt)
                    continue
                raise CanvasError(f"Falha de conexão com Canvas ({self.base_url}): {exc}") from exc

        raise CanvasError(f"Número máximo de tentativas excedido para {path}")

    async def _get_paginated(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        max_pages: int = 10
    ) -> List[Dict[str, Any]]:
        """Realiza requisições paginadas seguindo os cabeçalhos Link (rel='next')."""
        results: List[Dict[str, Any]] = []
        next_url: Optional[str] = path
        current_params = dict(params or {})
        current_params.setdefault("per_page", 50)

        page_count = 0
        while next_url and page_count < max_pages:
            page_count += 1
            # A partir da 2ª página, a URL do Link header já contém query params
            req_params = current_params if page_count == 1 else None
            response = await self._request("GET", next_url, params=req_params)

            data = response.json()
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                results.append(data)
                break

            # Processa paginação pelo cabeçalho Link
            link_header = response.headers.get("Link", "")
            links = parse_link_header(link_header)
            next_url = links.get("next")

        return results

    # -----------------------------------------------------------------
    # Implementação dos Contratos BaseLMSProvider
    # -----------------------------------------------------------------

    async def test_connection(self) -> bool:
        """Testa se o token é aceito pela API do Canvas."""
        if not self.api_token:
            return False

        try:
            # GET /api/v1/users/self/profile é o endpoint mais leve para checar credenciais
            resp = await self._request("GET", "/api/v1/users/self/profile")
            return resp.status_code == 200
        except (CanvasAuthenticationError, CanvasError):
            return False

    async def get_courses(self) -> List[LMSCourse]:
        """Obtém as disciplinas ativas do usuário no Canvas."""
        if not self.api_token:
            return []

        raw_courses = await self._get_paginated(
            "/api/v1/courses",
            params={
                "enrollment_state": "active",
                "include[]": "term"
            }
        )

        courses: List[LMSCourse] = []
        for c in raw_courses:
            name = c.get("name") or c.get("course_code")
            # Ignora cursos sem nome válido ou restritos
            if not name or not str(name).strip():
                continue

            course_id = str(c.get("id"))
            term_name = None
            if isinstance(c.get("term"), dict):
                term_name = c["term"].get("name")

            course_url = f"{self.base_url}/courses/{course_id}"

            courses.append(
                LMSCourse(
                    id=course_id,
                    name=name.strip(),
                    code=str(c.get("course_code") or course_id),
                    platform="canvas",
                    url=course_url,
                    term=term_name
                )
            )

        return courses

    async def get_upcoming_assignments(
        self,
        course_id: Optional[str] = None,
        days: int = 7
    ) -> List[LMSAssignment]:
        """Obtém tarefas acadêmicas do Canvas ordenadas por data limite de entrega."""
        if not self.api_token:
            return []

        # Se course_id foi especificado, consulta apenas ele; caso contrário, busca todos os ativos
        courses_to_query: List[LMSCourse] = []
        if course_id:
            courses_to_query = [
                LMSCourse(
                    id=str(course_id),
                    name="Disciplina Canvas",
                    code=str(course_id),
                    platform="canvas",
                    url=f"{self.base_url}/courses/{course_id}"
                )
            ]
        else:
            courses_to_query = await self.get_courses()

        all_assignments: List[LMSAssignment] = []
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days, hours=23, minutes=59) if days > 0 else None

        for course in courses_to_query:
            endpoint = f"/api/v1/courses/{course.id}/assignments"
            try:
                raw_assignments = await self._get_paginated(
                    endpoint,
                    params={
                        "bucket": "upcoming",
                        "order_by": "due_at"
                    }
                )

                for item in raw_assignments:
                    due_dt = parse_canvas_datetime(item.get("due_at"))

                    # Filtra por janela de dias se especificado
                    if cutoff and due_dt and due_dt > cutoff:
                        continue

                    # Identifica se já foi submetido pelo aluno
                    is_sub = bool(item.get("has_submitted_submissions", False))
                    sub_dict = item.get("submission") or {}
                    if isinstance(sub_dict, dict):
                        sub_state = str(sub_dict.get("workflow_state", "")).lower()
                        if sub_state in ("submitted", "graded"):
                            is_sub = True

                    assign_url = item.get("html_url") or f"{self.base_url}/courses/{course.id}/assignments/{item.get('id')}"

                    all_assignments.append(
                        LMSAssignment(
                            id=str(item.get("id")),
                            course_id=course.id,
                            course_name=course.name,
                            title=item.get("name", "Sem título"),
                            description=item.get("description") or "",
                            due_date=due_dt,
                            is_submitted=is_sub,
                            url=assign_url,
                            platform="canvas",
                            activity_type="quiz" if item.get("is_quiz_assignment") or item.get("quiz_id") else "assign",
                            points_possible=item.get("points_possible"),
                            submission_status="Enviado" if is_sub else "Pendente",
                            due_date_str=due_dt.strftime("%d/%m/%Y %H:%M") if due_dt else None
                        )
                    )
            except Exception as e:
                console.print(f"[yellow]⚠️ Erro ao consultar tarefas do curso {course.name} no Canvas: {e}[/yellow]")
                continue

        all_assignments.sort(key=lambda x: (x.due_date is None, x.due_date or datetime.max.replace(tzinfo=timezone.utc)))
        return all_assignments

    async def get_announcements(
        self,
        course_id: Optional[str] = None,
        limit: int = 5
    ) -> List[LMSAnnouncement]:
        """Obtém avisos e comunicados publicados nas disciplinas do Canvas."""
        if not self.api_token:
            return []

        courses_to_query: List[LMSCourse] = []
        if course_id:
            courses_to_query = [
                LMSCourse(
                    id=str(course_id),
                    name="Disciplina Canvas",
                    code=str(course_id),
                    platform="canvas",
                    url=f"{self.base_url}/courses/{course_id}"
                )
            ]
        else:
            courses_to_query = await self.get_courses()

        if not courses_to_query:
            return []

        context_codes = [f"course_{c.id}" for c in courses_to_query[:10]]
        course_name_map = {f"course_{c.id}": c.name for c in courses_to_query}

        params: Dict[str, Any] = {
            "context_codes[]": context_codes,
            "per_page": limit,
        }

        try:
            raw_announcements = await self._get_paginated("/api/v1/announcements", params=params, max_pages=2)
        except Exception as e:
            console.print(f"[yellow]⚠️ Erro ao consultar comunicados no Canvas: {e}[/yellow]")
            return []

        announcements: List[LMSAnnouncement] = []
        for a in raw_announcements[:limit]:
            ctx = a.get("context_code", "")
            c_name = course_name_map.get(ctx, "Disciplina Canvas")
            c_id = ctx.replace("course_", "") if ctx.startswith("course_") else ""
            posted_dt = parse_canvas_datetime(a.get("posted_at"))
            ann_url = a.get("html_url") or f"{self.base_url}/courses/{c_id}/discussion_topics/{a.get('id')}"

            announcements.append(
                LMSAnnouncement(
                    id=str(a.get("id")),
                    course_id=c_id,
                    course_name=c_name,
                    title=a.get("title", "Aviso"),
                    message=a.get("message", ""),
                    posted_at=posted_dt,
                    author=a.get("user_name") or "Professor / Monitor",
                    url=ann_url,
                    platform="canvas"
                )
            )

        return announcements

    async def get_assignment(
        self,
        course_id: str,
        assignment_id: str
    ) -> Optional[LMSAssignment]:
        """Obtém detalhes completos de uma atividade específica no Canvas (enunciado, prazos, etc.)."""
        if not self.api_token:
            return None

        clean_course_id, clean_assign_id = extract_canvas_ids(assignment_id, course_id)
        if not clean_assign_id:
            clean_assign_id = str(assignment_id)
        if not clean_course_id:
            clean_course_id = str(course_id)

        endpoint = f"/api/v1/courses/{clean_course_id}/assignments/{clean_assign_id}"
        try:
            resp = await self._request("GET", endpoint)
            item = resp.json()
            due_dt = parse_canvas_datetime(item.get("due_at"))
            is_sub = bool(item.get("has_submitted_submissions", False))
            sub_dict = item.get("submission") or {}
            if isinstance(sub_dict, dict):
                sub_state = str(sub_dict.get("workflow_state", "")).lower()
                if sub_state in ("submitted", "graded"):
                    is_sub = True

            assign_url = item.get("html_url") or f"{self.base_url}/courses/{clean_course_id}/assignments/{clean_assign_id}"

            desc = item.get("description") or ""
            desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()
            if not desc_clean:
                desc_clean = desc

            return LMSAssignment(
                id=str(item.get("id")),
                course_id=clean_course_id,
                course_name=item.get("course_name") or f"Curso {clean_course_id}",
                title=item.get("name", f"Atividade {clean_assign_id}"),
                description=desc_clean,
                due_date=due_dt,
                is_submitted=is_sub,
                url=assign_url,
                platform="canvas",
                activity_type="quiz" if item.get("is_quiz_assignment") or item.get("quiz_id") else "assign",
                points_possible=item.get("points_possible"),
                submission_status="Enviado" if is_sub else "Pendente",
                due_date_str=due_dt.strftime("%d/%m/%Y %H:%M") if due_dt else None
            )
        except Exception as e:
            console.print(f"[yellow]⚠️ Erro ao consultar atividade {clean_assign_id} no Canvas: {e}[/yellow]")
            return None

    async def sync_course_materials(self, course: LMSCourse) -> List[Path]:
        """Sincroniza e baixa arquivos e materiais da disciplina do Canvas para storage/materials/."""
        mat_dir = settings.STORAGE_MATERIALS_DIR / course.safe_name
        mat_dir.mkdir(parents=True, exist_ok=True)
        downloaded: List[Path] = []

        if not self.api_token:
            return downloaded

        # Consulta API /api/v1/courses/:id/files e baixa os arquivos
        try:
            raw_files = await self._get_paginated(f"/api/v1/courses/{course.id}/files")
            client = self._get_client()
            for f_info in raw_files:
                dl_url = f_info.get("url")
                fname = f_info.get("display_name") or f_info.get("filename") or f"arquivo_{f_info.get('id')}"
                if not dl_url:
                    continue

                dest = mat_dir / fname
                if not dest.exists():
                    try:
                        resp = await client.get(dl_url, follow_redirects=True)
                        if resp.status_code == 200:
                            dest.write_bytes(resp.content)
                            downloaded.append(dest)
                    except Exception as err:
                        console.print(f"[yellow]⚠️ Erro ao baixar {fname} do Canvas: {err}[/yellow]")
                else:
                    downloaded.append(dest)
        except Exception as e:
            console.print(f"[yellow]⚠️ Erro ao consultar materiais do curso {course.name} no Canvas: {e}[/yellow]")

        return downloaded

    async def scan_all(
        self,
        sync_materials: bool = True,
        sync_announcements: bool = True
    ) -> Tuple[List[LMSCourse], List[LMSAssignment], List[LMSAnnouncement]]:
        """Executa varredura completa de disciplinas, tarefas e comunicados do Canvas."""
        courses = await self.get_courses()
        assignments = await self.get_upcoming_assignments(days=15)
        announcements = await self.get_announcements(limit=10) if sync_announcements else []

        if sync_materials:
            for c in courses:
                try:
                    await self.sync_course_materials(c)
                except Exception:
                    pass

        return courses, assignments, announcements


# =====================================================================
# Utilitários de Submissão e Logs
# =====================================================================

async def _emit_log(callback: Optional[Any], msg: str):
    """Envia mensagem para o callback de log ao vivo de forma segura."""
    if not callback:
        return
    try:
        res = callback(msg)
        if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
            await res
    except Exception:
        pass


def extract_canvas_ids(url_or_id: str, course_id: Optional[str] = None) -> Tuple[str, str]:
    """Extrai course_id e assignment_id a partir de URLs ou identificadores do Canvas.

    Exemplos:
      - 'https://pucminas.instructure.com/courses/10101/assignments/20101' -> ('10101', '20101')
      - 'canvas_20101' com course_id='10101' -> ('10101', '20101')
    """
    c_id = str(course_id or "").strip()
    a_id = str(url_or_id or "").strip()

    if "courses/" in a_id:
        m = re.search(r"courses/(\d+)(?:/assignments/(\d+))?", a_id)
        if m:
            if m.group(1):
                c_id = m.group(1)
            if m.group(2):
                a_id = m.group(2)

    if a_id.startswith("canvas_"):
        a_id = a_id.replace("canvas_", "")

    return c_id, a_id


# =====================================================================
# Motor de Submissão Oficial na API REST do Canvas
# =====================================================================

class CanvasSubmitter:
    """Motor oficial de submissão de atividades na API REST do Canvas LMS (Instructure)."""

    def __init__(self, adapter: Optional[CanvasAdapter] = None):
        self.adapter = adapter or CanvasAdapter()

    async def submit_assignment(
        self,
        course_id: str,
        assignment_id: str,
        file_path: Path,
        comment: Optional[str] = None,
        on_log: Optional[Any] = None
    ) -> Tuple[bool, str]:
        """Submete um arquivo (PDF/DOCX) para uma atividade no Canvas LMS via API REST.

        Executa o fluxo oficial em 3 etapas da Instructure:
        1. Notificação de Upload: POST /courses/:id/assignments/:id/submissions/self/files
        2. Upload Binário: POST upload_url com multipart/form-data
        3. Finalização: POST /courses/:id/assignments/:id/submissions

        Args:
            course_id: ID do curso no Canvas.
            assignment_id: ID da atividade no Canvas.
            file_path: Caminho do arquivo a ser enviado.
            comment: Comentário opcional de entrega do aluno.
            on_log: Callback para mensagens de progresso ao vivo no Discord.

        Returns:
            Tuple[bool, str]: (sucesso, mensagem_ou_erro)
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return False, f"Arquivo não encontrado para submissão: {file_path}"

        clean_course_id, clean_assign_id = extract_canvas_ids(assignment_id, course_id)
        if not clean_assign_id:
            clean_assign_id = str(assignment_id)
        if not clean_course_id:
            clean_course_id = str(course_id)

        if not self.adapter.api_token:
            return False, "Token de acesso do Canvas LMS não configurado no .env (CANVAS_API_TOKEN)."

        client = self.adapter._get_client()

        try:
            # 1. Passo 1: Notificar o Canvas sobre o upload pretendido
            step1_endpoint = f"/api/v1/courses/{clean_course_id}/assignments/{clean_assign_id}/submissions/self/files"
            content_type = (
                "application/pdf"
                if file_path.suffix.lower() == ".pdf"
                else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            step1_payload = {
                "name": file_path.name,
                "size": file_path.stat().st_size,
                "content_type": content_type,
            }

            if on_log:
                await _emit_log(on_log, f"📡 Notificando upload no Canvas ({file_path.name})...")

            step1_resp = await self.adapter._request("POST", step1_endpoint, params=step1_payload)
            step1_data = step1_resp.json()

            upload_url = step1_data.get("upload_url")
            upload_params = step1_data.get("upload_params", {})

            if not upload_url:
                return False, f"Canvas não retornou upload_url no Passo 1: {step1_data}"

            # 2. Passo 2: Upload do arquivo binário para a upload_url
            if on_log:
                await _emit_log(on_log, "📤 Enviando binário do documento para o Canvas Storage...")

            # Canvas exige que os parâmetros de upload venham primeiro e 'file' por último
            file_bytes = file_path.read_bytes()
            data_fields = {k: str(v) for k, v in upload_params.items()}
            files = {"file": (file_path.name, file_bytes, content_type)}

            upload_resp = await client.post(upload_url, data=data_fields, files=files, timeout=60.0)

            # O Canvas pode retornar 200/201 ou 3xx redirect para confirmação
            file_obj = None
            if upload_resp.status_code in (301, 302, 303):
                loc = upload_resp.headers.get("Location")
                if loc:
                    confirm_resp = await client.get(loc)
                    file_obj = confirm_resp.json()
            elif upload_resp.status_code in (200, 201):
                file_obj = upload_resp.json()
            else:
                return False, f"Falha no upload do arquivo (HTTP {upload_resp.status_code}): {upload_resp.text}"

            file_id = None
            if isinstance(file_obj, dict):
                file_id = file_obj.get("id")

            if not file_id:
                return False, f"Arquivo enviado mas ID não retornado pelo Canvas: {file_obj}"

            # 3. Passo 3: Finalizar a submissão com os file_ids
            if on_log:
                await _emit_log(on_log, f"✅ Registrando submissão oficial da atividade (file_id: {file_id})...")

            step3_endpoint = f"/api/v1/courses/{clean_course_id}/assignments/{clean_assign_id}/submissions"
            step3_payload = {
                "submission[submission_type]": "online_upload",
                "submission[file_ids][]": [file_id],
            }
            if comment:
                step3_payload["comment[text_comment]"] = comment

            step3_resp = await self.adapter._request("POST", step3_endpoint, params=step3_payload)
            sub_result = step3_resp.json()

            sub_id = sub_result.get("id", "N/A")
            sub_date = sub_result.get("submitted_at", datetime.now().isoformat())

            return True, f"Entrega confirmada no Canvas! Protocolo ID: #{sub_id} em {sub_date}."

        except CanvasAuthenticationError as auth_err:
            return False, str(auth_err)
        except CanvasRateLimitError as rl_err:
            return False, str(rl_err)
        except Exception as exc:
            return False, f"Erro na submissão ao Canvas: {exc}"

