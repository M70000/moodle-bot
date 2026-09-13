"""Executor de tarefas despachadas pela Nuvem (Render Cloud Bridge Runner).

Executa em segundo plano no computador local do estudante, consumindo tarefas
aprovadas no Discord e executando as submissões reais no Moodle via Playwright.
"""

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from rich.console import Console

from config.settings import settings
from src.scraper.moodle_submitter import MoodleSubmitter

console = Console()


class BridgeRunner:
    """Consome a fila de submissão do Render e submete no Moodle localmente."""

    def __init__(self, render_url: str = ""):
        self.render_url = (render_url or settings.RENDER_URL or "").rstrip("/")
        self._is_running = False
        self._last_courses_publish: float = 0.0
        self._COURSES_PUBLISH_INTERVAL = 120.0  # Publica cursos a cada 2 minutos
        self._failed_materials = set()

    async def poll_once(self) -> int:
        """Consulta o Render e executa qualquer submissão pendente para este canal."""
        url_base = self.render_url or (settings.RENDER_URL or "").rstrip("/")
        if not url_base:
            return 0

        url = f"{url_base}/api/bridge/pending"

        loop = asyncio.get_running_loop()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MoodleDesktopRunner/1.0"})
            resp_bytes = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=8).read())
            data = json.loads(resp_bytes.decode("utf-8"))
            tasks = data.get("tasks", [])
        except Exception:
            return 0

        executed = 0
        for task in tasks:
            task_id = task.get("task_id")
            if not task_id:
                continue

            console.print(f"[bold green]📥 Tarefa recebida da Nuvem (ID: {task_id}):[/bold green] {task.get('title')} ({task.get('action')})")

            # Marca como in_progress no Render
            await self._claim_task(task_id, url_base)

            # Executa a ação localmente
            try:
                success, message = await self._execute_task(task)
            except Exception as exc:
                console.print(f"[bold red]❌ Erro inesperado ao executar tarefa {task_id}: {exc}[/bold red]")
                import traceback
                traceback.print_exc()
                success = False
                message = f"Erro inesperado no executor local: {exc}"

            # Reporta de volta ao Render para atualizar o Discord
            await self._report_complete(task_id, success, message, url_base)
            executed += 1

        # Publica lista de cursos e sincroniza materiais periodicamente (heartbeat de presença)
        import time
        now = time.time()
        if now - self._last_courses_publish > self._COURSES_PUBLISH_INTERVAL:
            await self.publish_courses_to_hub(url_base)
            await self.sync_custom_materials_from_hub(url_base)
            self._last_courses_publish = now

        return executed

    async def _claim_task(self, task_id: str, url_base: str):
        loop = asyncio.get_running_loop()
        try:
            url = f"{url_base}/api/bridge/claim"
            payload = json.dumps({"task_id": task_id}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5))
        except Exception:
            pass

    async def _report_complete(self, task_id: str, success: bool, message: str, url_base: str):
        loop = asyncio.get_running_loop()
        try:
            url = f"{url_base}/api/bridge/complete"
            payload = json.dumps({
                "task_id": task_id,
                "success": success,
                "message": message
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=8))
        except Exception as e:
            console.print(f"[red]Erro ao reportar conclusão da tarefa {task_id} ao Render: {e}[/red]")

    async def publish_courses_to_hub(self, url_base: str = "") -> bool:
        """Publica a lista de disciplinas, catálogo de tarefas e materiais no Render Hub (heartbeat de presença)."""
        base = url_base or self.render_url or (settings.RENDER_URL or "").rstrip("/")
        if not base:
            return False
        loop = asyncio.get_running_loop()
        try:
            from src.notifier.discord_bot import get_available_courses
            from src.scheduler.state import DaemonState
            courses = get_available_courses()
            try:
                state = DaemonState()
                assignments = state.data.get("assignments", {})
                raw_materials = state.get_custom_materials()
                custom_materials = [
                    m for m in raw_materials
                    if "magicmock" not in str(m).lower()
                ]
            except Exception:
                assignments = {}
                custom_materials = []

            url = f"{base}/api/bridge/sync"
            payload = json.dumps({
                "courses": courses,
                "assignments": assignments,
                "custom_materials": custom_materials
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "MoodleDesktopRunner/1.0"}
            )
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=8))
            console.print(f"[cyan]🔗 [Ponte] {len(courses)} disciplinas, {len(assignments)} tarefas e {len(custom_materials)} materiais sincronizados com o Hub.[/cyan]")
            return True
        except Exception as e:
            console.print(f"[yellow]Aviso ao publicar estado no Hub: {e}[/yellow]")
            return False

    async def sync_custom_materials_from_hub(self, url_base: str = "") -> int:
        """Sincroniza e baixa materiais enviados via Discord na Nuvem (Render Hub) para a máquina local."""
        base = url_base or self.render_url or (settings.RENDER_URL or "").rstrip("/")
        if not base:
            return 0

        loop = asyncio.get_running_loop()
        try:
            url = f"{base}/api/bridge/materials"
            req = urllib.request.Request(url, headers={"User-Agent": "MoodleDesktopRunner/1.0"})
            resp_bytes = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10).read())
            data = json.loads(resp_bytes.decode("utf-8"))
            mats = data.get("materials", [])
        except Exception:
            return 0

        if not mats:
            return 0

        downloaded = 0
        from src.notifier.discord_bot import resolve_course_materials_dir, sanitize_filename
        from src.scheduler.state import DaemonState
        state = DaemonState()

        for item in mats:
            course = item.get("course")
            filename = item.get("filename")
            att_url = item.get("attachment_url")
            if not course or not filename or not att_url:
                continue

            # Ignora materiais com MagicMock ou que falharam anteriormente com 403/404
            if (
                "magicmock" in str(item).lower()
                or not att_url.startswith("http")
                or att_url in self._failed_materials
            ):
                continue

            target_dir = resolve_course_materials_dir(course)
            target_file = target_dir / sanitize_filename(filename)

            if target_file.exists() and target_file.stat().st_size > 0:
                state.register_custom_material(
                    course=target_dir.name,
                    filename=target_file.name,
                    attachment_url=att_url,
                    channel_id=item.get("channel_id"),
                    message_id=item.get("message_id"),
                    uploader=item.get("uploader", ""),
                    size=item.get("size", 0)
                )
                continue

            try:
                console.print(f"[cyan]Baixando material adicionado via Discord: [bold]{filename}[/bold] ({course})...[/cyan]")
                req_file = urllib.request.Request(
                    att_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
                )
                content = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req_file, timeout=30).read())
                if content:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(content)
                    state.register_custom_material(
                        course=target_dir.name,
                        filename=target_file.name,
                        attachment_url=att_url,
                        channel_id=item.get("channel_id"),
                        message_id=item.get("message_id"),
                        uploader=item.get("uploader", ""),
                        size=len(content)
                    )
                    downloaded += 1
                    console.print(f"[green]✔ Material sincronizado no PC local:[/green] {target_file.name} em {target_dir.name}")
            except urllib.error.HTTPError as http_err:
                if http_err.code in (403, 404):
                    self._failed_materials.add(att_url)
                console.print(f"[yellow]Aviso ao baixar material {filename} da ponte: {http_err}[/yellow]")
            except Exception as dl_err:
                console.print(f"[yellow]Aviso ao baixar material {filename} da ponte: {dl_err}[/yellow]")

        return downloaded

    async def sync_state_to_hub(self, url_base: str = "") -> bool:
        """Alias conveniente para sincronização forçada de estado."""
        res = await self.publish_courses_to_hub(url_base)
        await self.sync_custom_materials_from_hub(url_base)
        return res

    async def _execute_task(self, task: dict):
        action = task.get("action")
        assignment_url = task.get("assignment_url")
        title = task.get("title", "Atividade")

        console.print(f"[bold green]📥 [Ponte Nuvem] Executando submissão aprovada no Discord: {title} ({action})[/bold green]")
        submitter = MoodleSubmitter()

        if action == "approve_assign":
            file_path_str = task.get("file_to_submit")
            if not file_path_str or not Path(file_path_str).exists():
                # Busca na pasta submissions se não tiver o caminho completo
                sub_dir = Path(settings.STORAGE_SUBMISSIONS_DIR)
                matches = list(sub_dir.glob(f"*{task.get('assignment_id')}*.pdf"))
                if matches:
                    file_path = matches[0]
                else:
                    return False, f"Arquivo PDF para a atividade {task.get('assignment_id')} não encontrado no disco local."
            else:
                file_path = Path(file_path_str)

            return await submitter.submit_assignment(assignment_url=assignment_url, file_path=file_path)

        elif action == "fill_quiz":
            answers = task.get("structured_answers") or {}
            return await submitter.submit_quiz(quiz_url=assignment_url, answers=answers, auto_submit=False)

        elif action == "finalize_quiz":
            answers = task.get("structured_answers") or {}
            return await submitter.submit_quiz(quiz_url=assignment_url, answers=answers, auto_submit=True)

        elif action in ("solve_task", "redo_task"):
            import discord
            from src.notifier.discord_bot import _execute_solve_flow, bot
            target_ch_id = int(task.get("channel_id") or 0)
            target_ch = None
            if target_ch_id:
                try:
                    if settings.DISCORD_BOT_TOKEN and not getattr(bot.http, "token", None):
                        await bot.login(settings.DISCORD_BOT_TOKEN)
                    if getattr(bot._connection, "_ready", None) is discord.utils.MISSING:
                        bot._connection._ready = asyncio.Event()
                        bot._connection._ready.set()
                    if bot.is_ready():
                        target_ch = bot.get_channel(target_ch_id)
                    if not target_ch and getattr(bot.http, "token", None):
                        target_ch = await asyncio.wait_for(bot.fetch_channel(target_ch_id), timeout=5.0)
                except Exception as ch_err:
                    console.print(f"[yellow]Nota ao obter canal do Discord {target_ch_id}: {ch_err}[/yellow]")

            async def _send_via_discord(*args, **kwargs):
                try:
                    if target_ch and hasattr(target_ch, "send"):
                        return await target_ch.send(*args, **kwargs)
                except Exception as ch_send_err:
                    console.print(f"[yellow]Nota ao enviar no canal {target_ch_id}: {ch_send_err}[/yellow]")

                if settings.DISCORD_CHANNEL_ID:
                    try:
                        ch = bot.get_channel(settings.DISCORD_CHANNEL_ID)
                        if not ch and getattr(bot.http, "token", None):
                            ch = await asyncio.wait_for(bot.fetch_channel(settings.DISCORD_CHANNEL_ID), timeout=5.0)
                        if ch and hasattr(ch, "send"):
                            return await ch.send(*args, **kwargs)
                    except Exception:
                        pass
                return None

            answers = task.get("structured_answers") or {}
            instrucoes = answers.get("instrucoes")
            extra_files_str = answers.get("extra_files") or []
            extra_paths = []
            for p_str in extra_files_str:
                p = Path(p_str)
                if p.exists() and p.is_file():
                    extra_paths.append(p)
                    continue

                filename = p.name
                course_name = task.get("course", "")
                from src.scraper.moodle_scraper import sanitize_filename
                candidates = []
                if course_name:
                    candidates.append(Path(settings.STORAGE_MATERIALS_PATH) / sanitize_filename(course_name) / filename)
                    candidates.append(Path(settings.STORAGE_MATERIALS_PATH) / course_name / filename)

                mat_root = Path(settings.STORAGE_MATERIALS_PATH)
                if mat_root.exists():
                    for found_path in mat_root.rglob(filename):
                        if found_path.is_file():
                            candidates.append(found_path)
                            break

                candidates.append(Path("storage/submissions/temp_uploads") / filename)

                for cand in candidates:
                    if cand.exists() and cand.is_file():
                        extra_paths.append(cand)
                        break

            if extra_paths:
                console.print(f"[cyan]📚 Materiais de apoio identificados no desktop ({len(extra_paths)}):[/cyan] {', '.join(p.name for p in extra_paths)}")
            elif extra_files_str:
                console.print(f"[yellow]⚠️ Materiais solicitados pela nuvem não encontrados localmente: {extra_files_str}[/yellow]")

            modo = answers.get("modo", "resolver")
            assignment_url = task.get("assignment_url", "")
            tarefa_target = assignment_url or answers.get("tarefa") or task.get("assignment_id") or task.get("title") or ""

            await _execute_solve_flow(
                send_func=_send_via_discord,
                tarefa=tarefa_target,
                instrucoes=instrucoes,
                extra_files=extra_paths,
                is_refazer=(action == "redo_task"),
                modo=modo,
                channel=target_ch
            )
            return True, f"Resolução de '{title}' concluída com sucesso pelo Desktop Runner."

        return False, f"Ação desconhecida: {action}"

    async def run_loop(self, poll_interval: float = 4.0):
        """Loop contínuo de polling da ponte."""
        self._is_running = True
        url_base = self.render_url or (settings.RENDER_URL or "").rstrip("/")
        console.print(f"[cyan]🔗 Ponte Nuvem (Render Hub) ativa para submissões remotas: {url_base}[/cyan]")

        # Publica disciplinas imediatamente ao iniciar (heartbeat inicial)
        if url_base:
            await self.publish_courses_to_hub(url_base)
            import time
            self._last_courses_publish = time.time()

        while self._is_running:
            try:
                await self.poll_once()
            except Exception:
                pass
            await asyncio.sleep(poll_interval)


bridge_runner = BridgeRunner()
