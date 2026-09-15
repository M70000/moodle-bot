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
from src.auth.canvas_auth import CanvasAuth

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
    """Converte strings ISO 8601 da API do Canvas em objetos datetime no fuso horário local."""
    if not dt_str:
        return None
    try:
        clean = dt_str.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        # Proteção Windows CRT: astimezone() lança [Errno 22] Invalid argument para anos < 1970
        if dt.year < 1970:
            return dt
        return dt.astimezone()
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
        auth_mode: Optional[str] = None,
        auth: Optional[CanvasAuth] = None,
        timeout: float = 30.0
    ):
        raw_url = base_url or getattr(settings, "CANVAS_BASE_URL", "https://pucminas.instructure.com")
        self.base_url = raw_url.rstrip("/")
        self.api_token = (
            api_token
            or os.environ.get("CANVAS_API_TOKEN")
            or getattr(settings, "CANVAS_API_TOKEN", "")
        ).strip()
        self.auth_mode = (
            auth_mode
            or os.environ.get("CANVAS_AUTH_MODE")
            or getattr(settings, "CANVAS_AUTH_MODE", "token")
        ).strip().lower()
        self.auth = auth or CanvasAuth(base_url=self.base_url)
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def platform(self) -> str:
        return "canvas"

    @property
    def is_configured(self) -> bool:
        """Verifica se o adaptador possui credenciais ou sessão configuradas."""
        if self.auth_mode == "token":
            return bool(self.api_token)
        if self.auth_mode == "credentials":
            return self.auth.session_exists or bool(
                getattr(settings, "CANVAS_USERNAME", "") and getattr(settings, "CANVAS_PASSWORD", "")
            )
        # cookies
        return self.auth.session_exists

    def _get_client(self) -> httpx.AsyncClient:
        """Cria ou reaproveita o cliente HTTP assíncrono com cabeçalhos padrão."""
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/json",
                "User-Agent": "LumiBot-CanvasAdapter/2.0",
            }
            cookies = None
            if self.auth_mode == "token":
                if self.api_token:
                    headers["Authorization"] = f"Bearer {self.api_token}"
            else:
                cookies = self.auth.get_cookies_dict()
                csrf = self.auth.get_csrf_token()
                if csrf:
                    headers["X-CSRF-Token"] = csrf

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                cookies=cookies,
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
                    if self.auth_mode == "credentials" and attempt <= retries:
                        console.print("[yellow]Sessão do Canvas expirada (401). Renovando automaticamente com credenciais...[/yellow]")
                        ok, _ = await self.auth.login_with_credentials(headless=True)
                        if ok:
                            await self.close()
                            client = self._get_client()
                            continue

                    if self.auth_mode == "token":
                        raise CanvasAuthenticationError(
                            "Autenticação no Canvas falhou (401 Unauthorized). "
                            "Verifique se a variável CANVAS_API_TOKEN no arquivo .env é válida."
                        )
                    elif self.auth_mode == "credentials":
                        raise CanvasAuthenticationError(
                            "Autenticação no Canvas falhou (401 Unauthorized). "
                            "Não foi possível autenticar ou renovar a sessão com CANVAS_USERNAME e CANVAS_PASSWORD."
                        )
                    else:
                        raise CanvasAuthenticationError(
                            "Sessão do Canvas expirada ou ausente (401 Unauthorized). "
                            "Realize o login no navegador pelo painel ou Discord para renovar os cookies."
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
        """Testa a conectividade e autenticação com o Canvas LMS."""
        if not self.is_configured:
            return False

        if self.auth_mode == "token":
            try:
                resp = await self._request("GET", "/api/v1/users/self/profile")
                return resp.status_code == 200
            except (CanvasAuthenticationError, CanvasError):
                return False

        if self.auth.session_exists:
            ok, _ = await self.auth.validate_session()
            if ok:
                return True

        if self.auth_mode == "credentials":
            ok, _ = await self.auth.login_with_credentials(headless=True)
            return ok

        return False

    async def get_courses(self) -> List[LMSCourse]:
        """Obtém as disciplinas ativas do usuário no Canvas."""
        if not self.is_configured:
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
        if not self.is_configured:
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
        now = datetime.now().astimezone()
        cutoff = now + timedelta(days=days, hours=23, minutes=59) if days > 0 else None

        for course in courses_to_query:
            endpoint = f"/api/v1/courses/{course.id}/assignments"
            try:
                # Inclui explicitamente o objeto 'submission' do estudante
                raw_assignments = await self._get_paginated(
                    endpoint,
                    params={
                        "include[]": "submission",
                        "order_by": "due_at"
                    }
                )

                for item in raw_assignments:
                    due_dt = parse_canvas_datetime(item.get("due_at"))

                    # Filtra por janela de dias se especificado (para tarefas futuras)
                    if cutoff and due_dt and due_dt > cutoff:
                        continue

                    # Identifica se o estudante ATUAL já enviou a resolução
                    # IMPORTANTE: 'has_submitted_submissions' indica se ALGUM aluno da turma enviou.
                    # NUNCA use 'has_submitted_submissions' para a entrega individual do estudante!
                    sub_dict = item.get("submission") or {}
                    is_sub = False
                    sub_status = "Não enviado"
                    grade_val = None

                    if isinstance(sub_dict, dict):
                        wf_state = str(sub_dict.get("workflow_state", "")).lower()
                        submitted_at = sub_dict.get("submitted_at")
                        attempt = sub_dict.get("attempt")

                        if sub_dict.get("grade") is not None:
                            grade_val = str(sub_dict.get("grade"))
                        elif sub_dict.get("score") is not None:
                            grade_val = str(sub_dict.get("score"))

                        # No Canvas, "unsubmitted" indica explicitamente que o aluno não enviou
                        if wf_state == "unsubmitted" or (submitted_at is None and attempt is None and wf_state != "graded"):
                            is_sub = False
                            sub_status = "Não enviado"
                        elif submitted_at is not None or wf_state in ("submitted", "graded", "pending_review"):
                            is_sub = True
                            sub_status = "Avaliado" if wf_state == "graded" else "Enviado"

                    # Calcula tempo restante formatado
                    time_rem = None
                    if due_dt:
                        diff = due_dt - datetime.now(due_dt.tzinfo)
                        if diff.total_seconds() < 0:
                            time_rem = "Prazo expirado"
                        else:
                            d_days = diff.days
                            d_hours, rem = divmod(diff.seconds, 3600)
                            d_mins, _ = divmod(rem, 60)
                            parts = []
                            if d_days > 0:
                                parts.append(f"{d_days} dia{'s' if d_days > 1 else ''}")
                            if d_hours > 0:
                                parts.append(f"{d_hours} hora{'s' if d_hours > 1 else ''}")
                            if d_mins > 0 or not parts:
                                parts.append(f"{d_mins} minuto{'s' if d_mins > 1 else ''}")
                            time_rem = " restante(s): " + " e ".join(parts)

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
                            submission_status=sub_status,
                            grade_value=grade_val,
                            time_remaining=time_rem,
                            due_date_str=due_dt.strftime("%d/%m/%Y às %H:%M") if (due_dt and due_dt.year >= 1900) else (due_dt.isoformat()[:16] if due_dt else None)
                        )
                    )
            except Exception as e:
                console.print(f"[yellow]⚠️ Erro ao consultar tarefas do curso {course.name} no Canvas: {e}[/yellow]")
                continue

        def _safe_canvas_sort(a: LMSAssignment):
            if a.due_date is None:
                return (1, float("inf"))
            try:
                # Se for timezone-aware, converte para UTC timestamp
                if a.due_date.tzinfo is not None:
                    return (0, a.due_date.astimezone(timezone.utc).timestamp())
                return (0, a.due_date.timestamp())
            except Exception:
                # Fallback para comparação por componentes de data
                return (0, float(a.due_date.year * 31536000 + a.due_date.month * 2592000 + a.due_date.day * 86400))

        all_assignments.sort(key=_safe_canvas_sort)
        return all_assignments

    async def get_announcements(
        self,
        course_id: Optional[str] = None,
        limit: int = 5
    ) -> List[LMSAnnouncement]:
        """Obtém avisos e comunicados publicados nas disciplinas do Canvas."""
        if not self.is_configured:
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
        if not self.is_configured:
            return None

        clean_course_id, clean_assign_id = extract_canvas_ids(assignment_id, course_id)
        if not clean_assign_id:
            clean_assign_id = str(assignment_id)
        if not clean_course_id:
            clean_course_id = str(course_id)

        endpoint = f"/api/v1/courses/{clean_course_id}/assignments/{clean_assign_id}"
        try:
            resp = await self._request("GET", endpoint, params={"include[]": "submission"})
            item = resp.json()
            due_dt = parse_canvas_datetime(item.get("due_at"))

            sub_dict = item.get("submission") or {}
            is_sub = False
            sub_status = "Não enviado"
            grade_val = None

            if isinstance(sub_dict, dict):
                wf_state = str(sub_dict.get("workflow_state", "")).lower()
                submitted_at = sub_dict.get("submitted_at")
                attempt = sub_dict.get("attempt")

                if sub_dict.get("grade") is not None:
                    grade_val = str(sub_dict.get("grade"))
                elif sub_dict.get("score") is not None:
                    grade_val = str(sub_dict.get("score"))

                if wf_state == "unsubmitted" or (submitted_at is None and attempt is None and wf_state != "graded"):
                    is_sub = False
                    sub_status = "Não enviado"
                elif submitted_at is not None or wf_state in ("submitted", "graded", "pending_review"):
                    is_sub = True
                    sub_status = "Avaliado" if wf_state == "graded" else "Enviado"

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
                submission_status=sub_status,
                grade_value=grade_val,
                due_date_str=due_dt.strftime("%d/%m/%Y às %H:%M") if due_dt else None
            )
        except Exception as e:
            console.print(f"[yellow]⚠️ Erro ao consultar atividade {clean_assign_id} no Canvas: {e}[/yellow]")
            return None

    async def _download_canvas_file(self, download_url: str, dest: Path) -> bool:
        """Baixa arquivo do Canvas com remoção segura de Authorization em redirecionamentos S3/CDN externos."""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            is_canvas_domain = any(dom in download_url for dom in ["instructure.com", "canvas", "pucminas.br"])

            headers = {"User-Agent": "LumiBot-CanvasAdapter/2.0"}
            if is_canvas_domain and self.api_token:
                headers["Authorization"] = f"Bearer {self.api_token}"

            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as dl_client:
                curr_url = download_url
                for _ in range(5):
                    req_headers = dict(headers)
                    # S3 e CloudFront rejeitam (HTTP 403) URLs pré-assinadas se houver cabeçalho Bearer Authorization
                    if not any(dom in curr_url for dom in ["instructure.com", "canvas", "pucminas.br"]):
                        req_headers.pop("Authorization", None)

                    resp = await dl_client.get(curr_url, headers=req_headers)
                    if resp.is_redirect:
                        curr_url = resp.headers.get("Location")
                        if not curr_url:
                            break
                        continue

                    if resp.status_code == 200:
                        dest.write_bytes(resp.content)
                        return True
                    else:
                        console.print(f"[yellow]⚠️ Falha HTTP {resp.status_code} ao baixar {dest.name}[/yellow]")
                        return False
        except Exception as dl_err:
            console.print(f"[yellow]⚠️ Erro no download de {dest.name}: {dl_err}[/yellow]")
            return False

        return False

    async def sync_course_materials(self, course: LMSCourse) -> List[Path]:
        """Sincroniza e baixa arquivos e materiais da disciplina do Canvas para storage/materials/."""
        mat_dir = settings.STORAGE_MATERIALS_DIR / course.safe_name
        mat_dir.mkdir(parents=True, exist_ok=True)
        downloaded: List[Path] = []

        if not self.is_configured:
            return downloaded

        from src.core.models import sanitize_filename
        files_to_download: Dict[str, Dict[str, Any]] = {}

        # 1. Consulta arquivos diretos da disciplina (/courses/:id/files)
        try:
            raw_files = await self._get_paginated(f"/api/v1/courses/{course.id}/files")
            for f in raw_files:
                f_id = str(f.get("id"))
                if f_id and f.get("url"):
                    files_to_download[f_id] = f
        except CanvasAPIError as ce:
            if ce.status_code in (401, 403):
                console.print(f"[dim]Aba Arquivos restrita pelo professor em '{course.name}'. Buscando materiais via Módulos...[/dim]")
            else:
                console.print(f"[yellow]Aviso ao listar arquivos de '{course.name}': {ce}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Aviso ao listar arquivos de '{course.name}': {e}[/yellow]")

        # 2. Busca materiais organizados em Módulos (/courses/:id/modules?include[]=items)
        try:
            raw_modules = await self._get_paginated(
                f"/api/v1/courses/{course.id}/modules",
                params={"include[]": "items"}
            )
            for mod in raw_modules:
                items = mod.get("items") or []
                for m_item in items:
                    if m_item.get("type") == "File":
                        content_id = str(m_item.get("content_id") or "")
                        if content_id and content_id not in files_to_download:
                            item_url = m_item.get("url")
                            file_details = None
                            if item_url:
                                try:
                                    resp = await self._request("GET", item_url)
                                    file_details = resp.json()
                                except Exception:
                                    pass
                            if not file_details:
                                try:
                                    resp = await self._request("GET", f"/api/v1/courses/{course.id}/files/{content_id}")
                                    file_details = resp.json()
                                except Exception:
                                    pass
                            if file_details and file_details.get("url"):
                                files_to_download[content_id] = file_details
        except Exception as mod_err:
            console.print(f"[yellow]Aviso ao consultar módulos de '{course.name}': {mod_err}[/yellow]")

        # 3. Executa o download com nomes sanitizados e proteção contra erros de diretório
        for f_id, f_info in files_to_download.items():
            dl_url = f_info.get("url")
            raw_name = f_info.get("display_name") or f_info.get("filename") or f"arquivo_{f_id}"
            clean_name = sanitize_filename(raw_name)
            if not clean_name.strip():
                clean_name = f"arquivo_{f_id}"

            dest = mat_dir / clean_name
            if not dest.exists():
                success = await self._download_canvas_file(dl_url, dest)
                if success:
                    downloaded.append(dest)
            else:
                downloaded.append(dest)

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

