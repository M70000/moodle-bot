import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.notifier.discord_bot import (
    BatchSelectView,
    ReviewActionView,
    bot,
    enqueue_solve_flow,
    extract_quiz_answers_payload,
)
from src.scheduler.queue_manager import QueueTaskType, queue_manager
from src.solver.gemini_solver import SolutionDraft
from src.scraper.moodle_scraper import Assignment


class TestBatchExecutionAndPipeline(unittest.TestCase):

    def test_extract_quiz_answers_payload_direct(self):
        # 1. Estruturado com 'key' e 'value'
        structured = [
            {"key": "Q1", "value": "A"},
            {"key": "Q2", "value": "Verdadeiro"},
        ]
        payload = extract_quiz_answers_payload(structured)
        self.assertEqual(payload, {"Q1": "A", "Q2": "Verdadeiro"})

        # 2. Estruturado com 'field' e 'value'
        structured_field = [
            {"field": "q1:1_answer", "value": "42"},
            {"field": "q2:1_answer", "value": "B"},
        ]
        payload = extract_quiz_answers_payload(structured_field)
        self.assertEqual(payload, {"q1:1_answer": "42", "q2:1_answer": "B"})

        # 3. Dicionário direto
        dict_payload = {"Q1": "C", "Q2": "Falso"}
        payload = extract_quiz_answers_payload(dict_payload)
        self.assertEqual(payload, dict_payload)

    def test_extract_quiz_answers_payload_markdown_fallback(self):
        # Simula arquivo Markdown de rascunho
        temp_dir = Path("storage/test_scratch")
        temp_dir.mkdir(parents=True, exist_ok=True)
        md_file = temp_dir / "draft_test_rascunho.md"

        content = """# Resolução do Questionário

### Questão 1
Enunciado da questão 1.
**Resposta:** Alternativa B
**Explicação:** Justificativa da questão.

### Questão 2
Enunciado da questão 2.
**Alternativa:** 12.5 rad/s
**Justificativa:** Cálculo detalhado.
"""
        md_file.write_text(content, encoding="utf-8")
        try:
            payload = extract_quiz_answers_payload(None, file_to_submit=md_file)
            self.assertEqual(payload.get("Q1"), "Alternativa B")
            self.assertEqual(payload.get("Q2"), "12.5 rad/s")
        finally:
            if md_file.exists():
                md_file.unlink()

    def test_review_action_view_states(self):
        # 1. Quiz padrão (não preenchido, não finalizado)
        view_default = ReviewActionView(
            assignment_id="101",
            assignment_url="https://moodle.ufmg.br/mod/quiz/view.php?id=101",
            activity_type="quiz",
            draft_saved=False,
            is_finalized=False
        )
        button_labels = [item.label for item in view_default.children if isinstance(item, discord.ui.Button)]
        self.assertIn("Apenas Preencher Quiz", button_labels)
        self.assertIn("Enviar Tudo e Terminar", button_labels)

        # 2. Quiz com rascunho preenchido no Moodle
        view_draft = ReviewActionView(
            assignment_id="101",
            assignment_url="https://moodle.ufmg.br/mod/quiz/view.php?id=101",
            activity_type="quiz",
            draft_saved=True,
            is_finalized=False
        )
        button_labels = [item.label for item in view_draft.children if isinstance(item, discord.ui.Button)]
        self.assertIn("✔ Respostas Preenchidas", button_labels)
        self.assertIn("Enviar Tudo e Terminar", button_labels)

        # 3. Quiz finalizado com sucesso
        view_finalized = ReviewActionView(
            assignment_id="101",
            assignment_url="https://moodle.ufmg.br/mod/quiz/view.php?id=101",
            activity_type="quiz",
            draft_saved=True,
            is_finalized=True
        )
        button_labels = [item.label for item in view_finalized.children if isinstance(item, discord.ui.Button)]
        self.assertIn("Enviado com Sucesso", button_labels)
        self.assertNotIn("Enviar Tudo e Terminar", button_labels)

    def test_enqueue_solve_flow_modes(self):
        mock_assignments = {
            "201": {
                "id": "201",
                "title": "Quiz de Teste",
                "course": "Física 3",
                "url": "https://moodle.ufmg.br/mod/quiz/view.php?id=201",
                "activity_type": "quiz"
            },
            "202": {
                "id": "202",
                "title": "Tarefa Escrita",
                "course": "Cálculo 1",
                "url": "https://moodle.ufmg.br/mod/assign/view.php?id=202",
                "activity_type": "assign"
            }
        }

        with patch("src.notifier.discord_bot.DaemonState") as MockState, \
             patch.object(queue_manager, "enqueue", new_callable=AsyncMock) as mock_enqueue:
            mock_state_inst = MagicMock()
            mock_state_inst.data = {"assignments": mock_assignments}
            MockState.return_value = mock_state_inst
            mock_enqueue.return_value = 1

            send_mock = AsyncMock()

            # 1. Modo Padrão ("resolver")
            asyncio.run(enqueue_solve_flow(send_mock, tarefa="201", modo="resolver"))
            task_item = mock_enqueue.call_args[0][0]
            self.assertEqual(task_item.task_type, QueueTaskType.RESOLVE_QUIZ)

            # 2. Modo Preencher ("preencher")
            asyncio.run(enqueue_solve_flow(send_mock, tarefa="201", modo="preencher"))
            task_item = mock_enqueue.call_args[0][0]
            self.assertEqual(task_item.task_type, QueueTaskType.PIPELINE_FILL)

            # 3. Modo Finalizar ("finalizar")
            asyncio.run(enqueue_solve_flow(send_mock, tarefa="201", modo="finalizar"))
            task_item = mock_enqueue.call_args[0][0]
            self.assertEqual(task_item.task_type, QueueTaskType.PIPELINE_COMPLETE)

    def test_batch_select_view_initialization(self):
        pending = [
            {"id": "301", "title": "Quiz 1", "course": "Química", "url": "https://moodle.ufmg.br/mod/quiz/1", "activity_type": "quiz", "due_date": "15/09/2026"},
            {"id": "302", "title": "Tarefa 2", "course": "Química", "url": "https://moodle.ufmg.br/mod/assign/2", "activity_type": "assign", "due_date": "16/09/2026"},
        ]

        view = BatchSelectView(pending_items=pending, disciplina_filter="Química", requester="TestUser")
        self.assertEqual(len(view.all_pending), 2)
        # Select menu deve ter 2 opções
        self.assertEqual(len(view.select_menu.options), 2)
        self.assertEqual(view.select_menu.options[0].value, "301")
        self.assertEqual(view.select_menu.options[1].value, "302")

        # Botões de modo presentes
        button_labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]
        self.assertIn("Apenas Resolver", button_labels)
        self.assertIn("Resolver e Preencher", button_labels)
        self.assertIn("Resolver e Enviar Tudo", button_labels)
        self.assertIn("Cancelar", button_labels)

    def test_batch_dispatch_execution(self):
        pending = [
            {"id": "401", "title": "Quiz 1", "course": "EDA", "url": "https://moodle.ufmg.br/mod/quiz/1", "activity_type": "quiz"},
            {"id": "402", "title": "Quiz 2", "course": "EDA", "url": "https://moodle.ufmg.br/mod/quiz/2", "activity_type": "quiz"},
        ]
        view = BatchSelectView(pending_items=pending, requester="TestUser")
        view.selected_ids = ["401", "402"]

        mock_interaction = MagicMock()
        mock_interaction.response.defer = AsyncMock()
        mock_interaction.channel.send = AsyncMock()
        mock_interaction.followup.send = AsyncMock()
        mock_interaction.user.display_name = "TestUser"
        mock_interaction.user.mention = "@TestUser"
        mock_interaction.message.edit = AsyncMock()

        with patch("src.notifier.discord_bot.enqueue_solve_flow", new_callable=AsyncMock) as mock_enqueue:
            mock_enqueue.side_effect = [1, 2]

            asyncio.run(view._dispatch_batch(mock_interaction, modo="finalizar"))

            self.assertEqual(mock_enqueue.call_count, 2)
            # Verifica que foram passados os IDs corretos e o modo "finalizar"
            first_call_kwargs = mock_enqueue.call_args_list[0].kwargs
            second_call_kwargs = mock_enqueue.call_args_list[1].kwargs

            self.assertEqual(first_call_kwargs["tarefa"], "401")
            self.assertEqual(first_call_kwargs["modo"], "finalizar")
            self.assertEqual(second_call_kwargs["tarefa"], "402")
            self.assertEqual(second_call_kwargs["modo"], "finalizar")

            # Verifica envio do embed de confirmação do lote
            self.assertTrue(mock_interaction.followup.send.called)
            sent_embed = mock_interaction.followup.send.call_args.kwargs.get("embed")
            self.assertIsNotNone(sent_embed)
            self.assertIn("Lote de Atividades Enfileirado com Sucesso!", sent_embed.title)

    def test_commands_registered(self):
        cmd_names = [cmd.name for cmd in bot.tree.get_commands()]
        self.assertIn("resolver", cmd_names)
        self.assertIn("resolver_lote", cmd_names)
        self.assertIn("refazer", cmd_names)

        prefix_cmds = [cmd.name for cmd in bot.commands]
        self.assertIn("resolver", prefix_cmds)
        self.assertIn("resolver_lote", prefix_cmds)
        self.assertIn("refazer", prefix_cmds)


if __name__ == "__main__":
    unittest.main()
