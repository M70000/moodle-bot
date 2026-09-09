import unittest
from unittest.mock import MagicMock, patch
from src.notifier.discord_bot import (
    _is_task_completed,
    pending_task_autocomplete,
    completed_task_autocomplete,
    bot
)

class TestResolverRefazer(unittest.TestCase):
    def test_is_task_completed_logic(self):
        # 1. Tarefa pendente
        pending_item = {
            "title": "Exercício 2",
            "is_submitted": False,
            "status": "pending_review",
            "submission_status": "Não enviado"
        }
        self.assertFalse(_is_task_completed(pending_item))

        # 2. Tarefa submetida por flag
        submitted_item = {
            "title": "Exercício 1",
            "is_submitted": True,
            "status": "submitted",
            "submission_status": "Enviado para avaliação"
        }
        self.assertTrue(_is_task_completed(submitted_item))

        # 3. Quiz concluído por texto de status
        completed_quiz = {
            "title": "Quiz Aula 1",
            "is_submitted": False,
            "status": "pending_review",
            "submission_status": "Concluído"
        }
        self.assertTrue(_is_task_completed(completed_quiz))

    def test_autocompletes_separation(self):
        mock_assignments = {
            "101": {
                "id": "101",
                "title": "Tarefa Pendente 1",
                "course": "Cálculo 2",
                "is_submitted": False,
                "status": "pending_review",
                "submission_status": "Não enviado"
            },
            "102": {
                "id": "102",
                "title": "Tarefa Concluída 2",
                "course": "Estatística",
                "is_submitted": True,
                "status": "submitted",
                "submission_status": "Enviado para avaliação"
            },
            "103": {
                "id": "103",
                "title": "Quiz Finalizado 3",
                "course": "Inglês Instrumental",
                "is_submitted": False,
                "status": "Concluído",
                "submission_status": "Concluído"
            }
        }

        mock_interaction = MagicMock()

        with patch("src.notifier.discord_bot.DaemonState") as MockState:
            mock_state_inst = MagicMock()
            mock_state_inst.data = {"assignments": mock_assignments}
            MockState.return_value = mock_state_inst

            import asyncio
            # Testa pending_task_autocomplete com string vazia e com None
            pending_choices = asyncio.run(pending_task_autocomplete(mock_interaction, ""))
            pending_ids = [c.value for c in pending_choices]
            self.assertIn("101", pending_ids)
            self.assertNotIn("102", pending_ids)
            self.assertNotIn("103", pending_ids)

            # Garante que None (enviado pelo Discord ao focar o campo) não quebra o autocomplete
            pending_none = asyncio.run(pending_task_autocomplete(mock_interaction, None))
            self.assertEqual(len(pending_none), 1)

            # Testa completed_task_autocomplete (usado em /refazer)
            completed_choices = asyncio.run(completed_task_autocomplete(mock_interaction, ""))
            completed_ids = [c.value for c in completed_choices]
            self.assertNotIn("101", completed_ids)
            self.assertIn("102", completed_ids)
            self.assertIn("103", completed_ids)

            # Garante que None não quebra completed_task_autocomplete
            completed_none = asyncio.run(completed_task_autocomplete(mock_interaction, None))
            self.assertEqual(len(completed_none), 2)

    def test_commands_registered(self):
        cmd_names = [cmd.name for cmd in bot.tree.get_commands()]
        self.assertIn("resolver", cmd_names)
        self.assertIn("refazer", cmd_names)
        self.assertIn("tarefas", cmd_names)
        self.assertIn("status", cmd_names)

if __name__ == "__main__":
    unittest.main()
