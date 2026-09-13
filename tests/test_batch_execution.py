import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.notifier.discord_bot import (
    BatchSelectView,
    ReviewActionView,
    _coordinate_batch_followup,
    _format_friendly_error,
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
        sample_mat = Path("storage/materials/Apoio_Quimica.pdf")

        view = BatchSelectView(
            pending_items=pending,
            disciplina_filter="Química",
            instrucoes="Resolver com foco em reações redox",
            attached_files=[sample_mat],
            available_materials=[sample_mat],
            requester="TestUser"
        )
        self.assertEqual(len(view.all_pending), 2)
        # Task select menu deve ter 2 opções
        self.assertEqual(len(view.task_select_menu.options), 2)
        self.assertEqual(view.task_select_menu.options[0].value, "301")
        self.assertEqual(view.task_select_menu.options[1].value, "302")

        # Material select menu deve existir e ter 1 opção
        self.assertIsNotNone(view.mat_select_menu)
        self.assertEqual(len(view.mat_select_menu.options), 1)
        self.assertEqual(view.mat_select_menu.options[0].value, sample_mat.name)

        # Instruções e anexos preservados
        self.assertEqual(view.instrucoes, "Resolver com foco em reações redox")
        self.assertEqual(view.attached_files, [sample_mat])

        # Botões de modo e instrução presentes
        button_labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]
        self.assertIn("Instruções", button_labels)
        self.assertIn("Apenas Resolver", button_labels)
        self.assertIn("Resolver e Preencher", button_labels)
        self.assertIn("Resolver e Enviar Tudo", button_labels)
        self.assertIn("Cancelar", button_labels)

        # Embed de exibição contém dados corretos
        embed = view.build_panel_embed()
        self.assertIn("Química", embed.title + (embed.description or ""))
        field_names = [f.name for f in embed.fields]
        self.assertTrue(any("Instruções" in fn for fn in field_names))
        self.assertTrue(any("Materiais" in fn for fn in field_names))

    def test_batch_dispatch_execution(self):
        pending = [
            {"id": "401", "title": "Quiz 1", "course": "EDA", "url": "https://moodle.ufmg.br/mod/quiz/1", "activity_type": "quiz"},
            {"id": "402", "title": "Quiz 2", "course": "EDA", "url": "https://moodle.ufmg.br/mod/quiz/2", "activity_type": "quiz"},
        ]
        sample_mat = Path("storage/materials/Algoritmos_Gabarito.pdf")
        view = BatchSelectView(
            pending_items=pending,
            instrucoes="Priorize respostas completas",
            attached_files=[sample_mat],
            requester="TestUser"
        )
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
            # Verifica que foram passados os IDs corretos, modo, instruções e arquivos anexados
            first_call_kwargs = mock_enqueue.call_args_list[0].kwargs
            second_call_kwargs = mock_enqueue.call_args_list[1].kwargs

            self.assertEqual(first_call_kwargs["tarefa"], "401")
            self.assertEqual(first_call_kwargs["modo"], "finalizar")
            self.assertEqual(first_call_kwargs["instrucoes"], "Priorize respostas completas")
            self.assertEqual(first_call_kwargs["extra_files"], [sample_mat])
            self.assertTrue(first_call_kwargs.get("silent_enqueue"))

            self.assertEqual(second_call_kwargs["tarefa"], "402")
            self.assertEqual(second_call_kwargs["modo"], "finalizar")
            self.assertEqual(second_call_kwargs["instrucoes"], "Priorize respostas completas")
            self.assertEqual(second_call_kwargs["extra_files"], [sample_mat])
            self.assertTrue(second_call_kwargs.get("silent_enqueue"))

            # Verifica envio do embed de confirmação do lote
            self.assertTrue(mock_interaction.followup.send.called)
            sent_embed = mock_interaction.followup.send.call_args.kwargs.get("embed")
            self.assertIn("Lote de Atividades Enfileirado com Sucesso!", sent_embed.title)

    def test_enqueue_solve_flow_silent_enqueue(self):
        """Verifica que silent_enqueue=True não dispara mensagem individual de fila quando pos > 1."""
        mock_assignments = {
            "201": {
                "id": "201",
                "title": "Quiz de Teste",
                "course": "Física 3",
                "url": "https://moodle.ufmg.br/mod/quiz/view.php?id=201",
                "activity_type": "quiz"
            }
        }
        with patch("src.notifier.discord_bot.DaemonState") as MockState, \
             patch.object(queue_manager, "enqueue", new_callable=AsyncMock) as mock_enqueue:
            mock_state_inst = MagicMock()
            mock_state_inst.data = {"assignments": mock_assignments}
            MockState.return_value = mock_state_inst
            mock_enqueue.return_value = 2

            send_mock = AsyncMock()

            # 1. Com silent_enqueue=True -> send_mock não deve ser chamado para alertar a fila
            pos = asyncio.run(enqueue_solve_flow(send_mock, tarefa="201", modo="resolver", silent_enqueue=True))
            self.assertEqual(pos, 2)
            self.assertFalse(send_mock.called)

            # 2. Com silent_enqueue=False (padrão) -> send_mock deve ser chamado
            pos = asyncio.run(enqueue_solve_flow(send_mock, tarefa="201", modo="resolver", silent_enqueue=False))
            self.assertEqual(pos, 2)
            self.assertTrue(send_mock.called)

    def test_resolve_channel_fallback_http(self):
        """Verifica que _resolve_channel tenta fetch_channel mesmo quando bot.is_ready() é False."""
        from src.notifier.discord_bot import MoodleDiscordNotifier
        notifier = MoodleDiscordNotifier(token="mock_token", channel_id=987654)

        with patch.object(bot, "is_ready", return_value=False), \
             patch.object(bot, "fetch_channel", new_callable=AsyncMock) as mock_fetch:
            mock_channel = MagicMock()
            mock_fetch.return_value = mock_channel

            res = asyncio.run(notifier._resolve_channel(987654))
            self.assertEqual(res, mock_channel)
            mock_fetch.assert_called_once_with(987654)

    def test_commands_registered(self):
        cmd_names = [cmd.name for cmd in bot.tree.get_commands()]
        self.assertIn("resolver", cmd_names)
        self.assertIn("resolver_lote", cmd_names)
        self.assertIn("refazer", cmd_names)

        prefix_cmds = [cmd.name for cmd in bot.commands]
        self.assertIn("resolver", prefix_cmds)
        self.assertIn("resolver_lote", prefix_cmds)
        self.assertIn("refazer", prefix_cmds)


    def test_format_friendly_error(self):
        """Testa a conversão de exceções técnicas em explicações claras ao usuário."""
        # 503
        self.assertIn("503", _format_friendly_error("RuntimeError: 503 Service Unavailable / High demand"))
        # 429
        self.assertIn("429", _format_friendly_error("RESOURCE_EXHAUSTED: quota exceeded"))
        # Timeout
        self.assertIn("Tempo limite", _format_friendly_error("asyncio.exceptions.TimeoutError"))
        # Moodle quiz closed
        self.assertIn("inacessível", _format_friendly_error("Não foi possível abrir ou retomar a tentativa"))
        # Genérico
        self.assertEqual(_format_friendly_error("Erro de conexão genérico"), "Erro de conexão genérico")

    def test_batch_coordinator_all_success(self):
        """Testa o coordenador de lote quando todas as tarefas concluem com sucesso."""
        chosen = [
            {"id": "501", "title": "Quiz 1", "course": "Cálculo 1"},
            {"id": "502", "title": "Tarefa 2", "course": "Cálculo 1"}
        ]
        ev1, ev2 = asyncio.Event(), asyncio.Event()
        ev1.set()
        ev2.set()
        done_events = {"501": ev1, "502": ev2}
        records = {
            "501": {"item": chosen[0], "success": True, "message": "OK", "error": None, "retried": False},
            "502": {"item": chosen[1], "success": True, "message": "OK", "error": None, "retried": False},
        }
        send_mock = AsyncMock()

        asyncio.run(_coordinate_batch_followup(
            records=records,
            done_events=done_events,
            chosen_items=chosen,
            batch_send=send_mock,
            modo="resolver",
            instrucoes=None,
            combined_files=[],
            requester="User",
            channel=None,
            user_mention="@User"
        ))

        # Deve enviar 1 relatório final de sucesso
        self.assertTrue(send_mock.called)
        embed = send_mock.call_args.kwargs.get("embed")
        self.assertIn("Sucesso Total", embed.title)
        self.assertEqual(embed.color, discord.Color.green())

    def test_batch_coordinator_auto_retry_recovers_503(self):
        """Testa o coordenador disparando auto-retry após erro 503 e reportando recuperação."""
        chosen = [
            {"id": "601", "title": "Quiz 1", "course": "Física"},
            {"id": "602", "title": "Quiz 2", "course": "Física"}
        ]
        ev1, ev2 = asyncio.Event(), asyncio.Event()
        ev1.set()
        ev2.set()
        done_events = {"601": ev1, "602": ev2}
        records = {
            "601": {"item": chosen[0], "success": True, "message": "OK", "error": None, "retried": False},
            "602": {"item": chosen[1], "success": False, "message": "503 Unavailable", "error": "503 Unavailable", "retried": False},
        }
        send_mock = AsyncMock()

        with patch("src.notifier.discord_bot.enqueue_solve_flow", new_callable=AsyncMock) as mock_retry_enqueue, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

            # Simula sucesso na repescagem para a tarefa 602
            async def fake_enqueue(*args, **kwargs):
                on_fin = kwargs.get("on_finish")
                if on_fin:
                    await on_fin(True, "Recuperado na 2ª tentativa")
                return 1

            mock_retry_enqueue.side_effect = fake_enqueue

            asyncio.run(_coordinate_batch_followup(
                records=records,
                done_events=done_events,
                chosen_items=chosen,
                batch_send=send_mock,
                modo="resolver",
                instrucoes=None,
                combined_files=[],
                requester="User",
                channel=None,
                user_mention="@User"
            ))

            # Verifica se enqueue_solve_flow foi chamado para a tarefa 602 (retry)
            mock_retry_enqueue.assert_called_once()
            self.assertEqual(mock_retry_enqueue.call_args.kwargs["tarefa"], "602")
            mock_sleep.assert_called_once_with(10.0)

            # Verifica o relatório final com recuperação
            last_embed = send_mock.call_args_list[-1].kwargs.get("embed")
            self.assertIn("Sucesso Total", last_embed.title)
            field_text = "".join(f.value for f in last_embed.fields)
            self.assertIn("recuperada na repescagem", field_text)

    def test_batch_coordinator_persistent_failure_state_preservation(self):
        """Testa falha persistente mantendo a tarefa como pendente e indicando erro no relatório."""
        from src.scheduler.state import DaemonState
        state = DaemonState()
        state.data["assignments"]["701"] = {
            "id": "701",
            "title": "Trabalho Difícil",
            "course": "Álgebra",
            "is_submitted": False,
            "status": "pending_review"
        }
        state.save()

        chosen = [{"id": "701", "title": "Trabalho Difícil", "course": "Álgebra"}]
        ev1 = asyncio.Event()
        ev1.set()
        done_events = {"701": ev1}
        records = {
            "701": {"item": chosen[0], "success": False, "message": "503 Unavailable", "error": "503 Unavailable", "retried": False}
        }
        send_mock = AsyncMock()

        with patch("src.notifier.discord_bot.enqueue_solve_flow", new_callable=AsyncMock) as mock_retry_enqueue, \
             patch("asyncio.sleep", new_callable=AsyncMock):

            # Continua falhando no retry
            async def fake_enqueue(*args, **kwargs):
                on_fin = kwargs.get("on_finish")
                if on_fin:
                    await on_fin(False, "503 Still Unavailable")
                return 1

            mock_retry_enqueue.side_effect = fake_enqueue

            asyncio.run(_coordinate_batch_followup(
                records=records,
                done_events=done_events,
                chosen_items=chosen,
                batch_send=send_mock,
                modo="resolver",
                instrucoes=None,
                combined_files=[],
                requester="User",
                channel=None,
                user_mention="@User"
            ))

            last_embed = send_mock.call_args_list[-1].kwargs.get("embed")
            self.assertIn("Falha", last_embed.title)
            field_text = "".join(f.value for f in last_embed.fields)
            self.assertIn("503", field_text)
            self.assertIn("permanecem como pendentes", field_text)

            # Verifica que a tarefa continua pendente no DaemonState
            st = DaemonState()
            item_st = st.get_assignment("701")
            self.assertFalse(item_st.get("is_submitted", False))
            self.assertNotEqual(item_st.get("status"), "submitted")

    def test_queue_channel_auto_discovery(self):
        """Testa o auto-descobrimento do canal fila-tarefas pelo TaskQueueManager."""
        mock_bot = MagicMock()
        mock_guild = MagicMock()
        mock_queue_ch = MagicMock()
        mock_queue_ch.id = 555444333
        mock_queue_ch.name = "fila-tarefas"
        mock_other_ch = MagicMock()
        mock_other_ch.id = 111222333
        mock_other_ch.name = "geral"

        mock_guild.text_channels = [mock_other_ch, mock_queue_ch]
        mock_bot.guilds = [mock_guild]
        mock_bot.get_channel = MagicMock(return_value=None)

        queue_manager.set_bot(mock_bot)
        queue_manager._dashboard_channel_id = 0

        # Resolução automática via _resolve_target_channel
        res_ch = asyncio.run(queue_manager._resolve_target_channel())
        self.assertEqual(res_ch, mock_queue_ch)
        self.assertEqual(queue_manager._dashboard_channel_id, 555444333)


if __name__ == "__main__":
    unittest.main()
