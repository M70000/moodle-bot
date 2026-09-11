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

    async def poll_once(self) -> int:
        """Consulta o Render e executa qualquer submissão pendente para este canal."""
        url_base = self.render_url or (settings.RENDER_URL or "").rstrip("/")
        if not url_base:
            return 0

        channel_id = str(settings.DISCORD_CHANNEL_ID or 0)
        url = f"{url_base}/api/bridge/pending"
        if channel_id and channel_id != "0":
            url += f"?channel_id={channel_id}"

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

            # Marca como in_progress no Render
            await self._claim_task(task_id, url_base)

            # Executa a ação localmente
            success, message = await self._execute_task(task)

            # Reporta de volta ao Render para atualizar o Discord
            await self._report_complete(task_id, success, message, url_base)
            executed += 1

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

        return False, f"Ação desconhecida: {action}"

    async def run_loop(self, poll_interval: float = 4.0):
        """Loop contínuo de polling da ponte."""
        self._is_running = True
        console.print(f"[cyan]🔗 Ponte Nuvem (Render Hub) ativa para submissões remotas: {self.render_url or settings.RENDER_URL}[/cyan]")
        while self._is_running:
            try:
                await self.poll_once()
            except Exception:
                pass
            await asyncio.sleep(poll_interval)


bridge_runner = BridgeRunner()
