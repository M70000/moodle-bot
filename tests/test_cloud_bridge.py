"""Testes unitários para a Ponte Nuvem (CloudBridgeManager e BridgeRunner)."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.notifier.bridge_manager import CloudBridgeManager
from src.scheduler.bridge_runner import BridgeRunner


class TestCloudBridge(unittest.IsolatedAsyncioTestCase):
    """Testa o ciclo de vida de tarefas despachadas pela ponte."""

    async def test_dispatch_and_complete_task(self):
        manager = CloudBridgeManager()

        task_id = await manager.dispatch_action(
            action="approve_assign",
            assignment_id="12345",
            assignment_url="https://virtual.ufmg.br/mod/assign/view.php?id=12345",
            channel_id="998877",
            message_id="112233",
            requester="AlunoTeste",
            title="Lista de Teste 1",
            course="Eletrônica",
            file_to_submit="storage/submissions/test.pdf"
        )

        self.assertTrue(task_id.startswith("bridge_"))

        # Recupera pendentes para o canal
        tasks = await manager.get_pending_tasks(channel_id="998877")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], task_id)
        self.assertEqual(tasks[0]["action"], "approve_assign")

        # Marca como in_progress
        await manager.mark_in_progress(task_id)

        # Completa a tarefa
        completed = await manager.complete_task(task_id, success=True, message="Enviado com sucesso no Moodle!")
        self.assertIsNotNone(completed)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["success"])

        # Verifica se não há mais pendentes
        pending_after = await manager.get_pending_tasks(channel_id="998877")
        self.assertEqual(len(pending_after), 0)

    @patch("src.scheduler.bridge_runner.urllib.request.urlopen")
    async def test_bridge_runner_poll_and_execute(self, mock_urlopen):
        """Testa o polling e execução pelo BridgeRunner."""
        # Simula resposta do Render com uma tarefa pendente
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"tasks": [{"task_id": "bridge_abc123", "action": "approve_assign", "assignment_id": "555", "assignment_url": "https://moodle/555", "channel_id": "111", "title": "Lista 5"}]}'
        )
        mock_urlopen.return_value = mock_resp

        runner = BridgeRunner(render_url="https://mock-app.onrender.com")

        with patch.object(runner, "_execute_task", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = (True, "Submissão confirmada")
            with patch.object(runner, "_claim_task", new_callable=AsyncMock):
                with patch.object(runner, "_report_complete", new_callable=AsyncMock):
                    count = await runner.poll_once()
                    self.assertEqual(count, 1)
                    mock_exec.assert_called_once()

    async def test_publish_state_courses_and_assignments(self):
        """Testa sincronização de disciplinas e catálogo de tarefas na ponte nuvem."""
        manager = CloudBridgeManager()

        courses = ["Cálculo 1", "Física 2", "Estatística"]
        assignments = {
            "101": {"id": "101", "title": "Lista 1", "course": "Cálculo 1"},
            "102": {"id": "102", "title": "Questionário 2", "course": "Física 2"}
        }

        await manager.publish_state(courses=courses, assignments=assignments)

        pub_courses = await manager.get_published_courses()
        pub_assign = await manager.get_published_assignments()
        status = await manager.desktop_status()

        self.assertEqual(pub_courses, courses)
        self.assertEqual(len(pub_assign), 2)
        self.assertEqual(pub_assign["101"]["title"], "Lista 1")
        self.assertTrue(status["online"])
        self.assertEqual(status["courses_count"], 3)
        self.assertEqual(status["assignments_count"], 2)

    @patch("src.scheduler.bridge_runner.urllib.request.urlopen")
    async def test_bridge_runner_solve_task_execution(self, mock_urlopen):
        """Testa o despacho e execução de solve_task pelo BridgeRunner no Desktop."""
        runner = BridgeRunner(render_url="https://mock-app.onrender.com")

        task = {
            "task_id": "bridge_solv1",
            "action": "solve_task",
            "assignment_id": "999",
            "title": "Trabalho 1",
            "course": "2026_2 - INGLÊS INSTRUMENTAL I - METATURMA",
            "channel_id": "123",
            "structured_answers": {
                "instrucoes": "resolver com calma",
                "modo": "resolver",
                "extra_files": ["/opt/render/project/src/storage/materials/Calc/resumo.pdf"]
            }
        }

        with patch("src.notifier.discord_bot._execute_solve_flow", new_callable=AsyncMock) as mock_solve, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True):
            success, msg = await runner._execute_task(task)
            self.assertTrue(success)
            self.assertIn("Trabalho 1", msg)
            mock_solve.assert_called_once()
            _, kwargs = mock_solve.call_args
            self.assertTrue(len(kwargs.get("extra_files", [])) >= 1)

    async def test_bridge_runner_relogin_task(self):
        """Testa o despacho e execução de relogin pelo BridgeRunner no Desktop."""
        runner = BridgeRunner(render_url="https://mock-app.onrender.com")
        task = {
            "task_id": "bridge_relog1",
            "action": "relogin",
            "title": "Renovação Interativa de Login no Moodle",
            "channel_id": "123"
        }

        mock_auth = MagicMock()
        mock_auth.interactive_login = AsyncMock(return_value=True)
        mock_auth.validate_session = AsyncMock(return_value=(True, "Usuario Logado"))

        with patch("src.auth.moodle_auth.MoodleAuth", return_value=mock_auth), \
             patch("src.notifier.discord_bot.MoodleDiscordNotifier.send_session_renewed_notification", new_callable=AsyncMock) as mock_notify:
            success, msg = await runner._execute_task(task)
            self.assertTrue(success)
            self.assertIn("renovada com sucesso", msg.lower())
            mock_auth.interactive_login.assert_called_once_with(headless=False)
            mock_notify.assert_called_once_with(user_name="Usuario Logado")


if __name__ == "__main__":
    unittest.main()
