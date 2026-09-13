import unittest
from unittest.mock import AsyncMock, MagicMock, patch
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

    def test_modificar_button_present_in_quiz_and_assignment(self):
        """Garante que o botão 'Modificar' está presente tanto em questionários quanto em trabalhos."""
        import discord
        from src.notifier.discord_bot import ReviewActionView
        from src.solver.gemini_solver import SolutionDraft
        from pathlib import Path

        draft = SolutionDraft(
            assignment_id="555",
            assignment_title="Cálculo 1 - Lista 3",
            course_name="Cálculo 1",
            summary="Resumo inicial",
            full_markdown="### Questão 1\nResposta inicial.",
            output_path=Path("storage/test_draft.md"),
            activity_type="assign"
        )

        # 1. Tarefa (assign)
        view_assign = ReviewActionView(
            assignment_id="555",
            assignment_url="https://moodle.ufmg.br/mod/assign/view.php?id=555",
            activity_type="assign",
            draft=draft
        )
        labels_assign = [b.label for b in view_assign.children if isinstance(b, discord.ui.Button)]
        self.assertIn("Modificar", labels_assign)
        self.assertIn("Aprovar e Enviar", labels_assign)

        # 2. Questionário (quiz) não preenchido
        view_quiz = ReviewActionView(
            assignment_id="666",
            assignment_url="https://moodle.ufmg.br/mod/quiz/view.php?id=666",
            activity_type="quiz",
            draft=draft
        )
        labels_quiz = [b.label for b in view_quiz.children if isinstance(b, discord.ui.Button)]
        self.assertIn("Modificar", labels_quiz)
        self.assertIn("Apenas Preencher Quiz", labels_quiz)
        self.assertIn("Enviar Tudo e Terminar", labels_quiz)

        # 3. Questionário (quiz) com rascunho preenchido
        view_quiz_draft = ReviewActionView(
            assignment_id="666",
            assignment_url="https://moodle.ufmg.br/mod/quiz/view.php?id=666",
            activity_type="quiz",
            draft_saved=True,
            draft=draft
        )
        labels_quiz_draft = [b.label for b in view_quiz_draft.children if isinstance(b, discord.ui.Button)]
        self.assertIn("Modificar", labels_quiz_draft)

        # 4. Finalizado (não deve exibir modificar)
        view_finalized = ReviewActionView(
            assignment_id="666",
            assignment_url="https://moodle.ufmg.br/mod/quiz/view.php?id=666",
            activity_type="quiz",
            is_finalized=True,
            draft=draft
        )
        labels_finalized = [b.label for b in view_finalized.children if isinstance(b, discord.ui.Button)]
        self.assertNotIn("Modificar", labels_finalized)
        self.assertIn("Enviado com Sucesso", labels_finalized)

    def test_revision_modal_structure_and_flow(self):
        """Verifica a inicialização e envio do modal de modificação."""
        import asyncio
        from unittest.mock import AsyncMock
        from src.notifier.discord_bot import RevisionModal
        from src.solver.gemini_solver import SolutionDraft
        from pathlib import Path

        draft = SolutionDraft(
            assignment_id="777",
            assignment_title="Física Experimental - Relatório 1",
            course_name="Física Experimental",
            summary="Resumo inicial",
            full_markdown="### Questão 1\nCálculo inicial de gravidade.",
            output_path=Path("storage/test_draft.md"),
            activity_type="assign"
        )

        modal = RevisionModal(
            draft=draft,
            assignment_id="777",
            assignment_url="https://moodle.ufmg.br/mod/assign/view.php?id=777",
            title="✏️ Modificar Resolução",
            activity_type="assign"
        )

        self.assertEqual(modal.title, "✏️ Modificar Resolução")
        self.assertEqual(modal.revision_input.label, "O que deseja alterar no arquivo/resolução?")
        self.assertEqual(modal.revision_input.min_length, 3)

        # Mock de submissão do modal
        mock_interaction = MagicMock()
        mock_interaction.response.defer = AsyncMock()
        mock_interaction.followup.send = AsyncMock(return_value=MagicMock())
        mock_interaction.channel = MagicMock()
        modal.revision_input._value = "Altere o valor de g para 9.81 m/s² na questão 1."

        with patch("src.solver.ai_solver.AISolver.apply_revision", new_callable=AsyncMock) as mock_apply, \
             patch("src.notifier.discord_bot.MoodleDiscordNotifier.send_assignment_review", new_callable=AsyncMock) as mock_send_review:
            
            updated_draft = SolutionDraft(
                assignment_id="777",
                assignment_title="Física Experimental - Relatório 1",
                course_name="Física Experimental",
                summary="Resumo atualizado com g=9.81",
                full_markdown="### Questão 1\nCálculo com g = 9.81 m/s².",
                output_path=Path("storage/test_draft_v2.md"),
                docx_path=Path("storage/test_draft_v2.docx"),
                activity_type="assign"
            )
            mock_apply.return_value = updated_draft
            mock_send_review.return_value = True

            asyncio.run(modal.on_submit(mock_interaction))

            self.assertTrue(mock_apply.called)
            self.assertEqual(mock_apply.call_args[1]["revision_instructions"], "Altere o valor de g para 9.81 m/s² na questão 1.")
            self.assertTrue(mock_send_review.called)
            self.assertEqual(mock_send_review.call_args[1]["draft"], updated_draft)
            self.assertEqual(mock_send_review.call_args[1]["channel"], mock_interaction.channel)

    def test_find_assignment_by_query_precision_and_safety(self):
        """Verifica a precisão determinística e segurança do find_assignment_by_query."""
        from src.notifier.discord_bot import find_assignment_by_query

        sample_assignments = {
            "102068": {
                "id": "102068",
                "title": "Exercício 1 - Estatística Descritiva",
                "course": "2026_2 - FUNDAMENTOS DE ESTATÍSTICA E CIÊNCIA DE DADOS - TB1",
                "url": "https://virtual.ufmg.br/20262/mod/assign/view.php?id=102068",
                "activity_type": "assign"
            },
            "45759": {
                "id": "45759",
                "title": "Unidade 7 :: Aula 3",
                "course": "2026_2 - INGLÊS INSTRUMENTAL I - METATURMA",
                "url": "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45759",
                "activity_type": "quiz"
            },
            "45695": {
                "id": "45695",
                "title": "Unidade 1 :: Aula 3",
                "course": "2026_2 - INGLÊS INSTRUMENTAL I - METATURMA",
                "url": "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45695",
                "activity_type": "quiz"
            }
        }

        # 1. Match exato por ID
        r = find_assignment_by_query("45759", sample_assignments)
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "45759")

        # 2. Match exato por título
        r = find_assignment_by_query("Unidade 7 :: Aula 3", sample_assignments)
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "45759")

        # 3. Match por rótulo do autocomplete com disciplina (caso real que gerou o bug)
        r = find_assignment_by_query("Unidade 7 :: Aula 3 - Inglês Instrumental I", sample_assignments)
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "45759")

        # 4. Match com sufixo de prazo
        r = find_assignment_by_query("Unidade 7 :: Aula 3 - Inglês Instrumental I (Prazo: 7 de janeiro)", sample_assignments)
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "45759")

        # 5. Match com sufixo [Concluído]
        r = find_assignment_by_query("Unidade 7 :: Aula 3 [Concluído]", sample_assignments)
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "45759")

        # 6. Match por URL direta com id=
        r = find_assignment_by_query("https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45759", sample_assignments)
        self.assertIsNotNone(r)
        self.assertEqual(r["id"], "45759")

        # 7. SEGURANÇA CRÍTICA: URLs base/genéricas NUNCA devem casar com o primeiro item
        r_base1 = find_assignment_by_query("https://virtual.ufmg.br/20262", sample_assignments)
        self.assertIsNone(r_base1)

        r_base2 = find_assignment_by_query("https://virtual.ufmg.br", sample_assignments)
        self.assertIsNone(r_base2)

        # 8. Tarefa de outra disciplina não se confunde
        r_est = find_assignment_by_query("Exercício 1 - Estatística Descritiva", sample_assignments)
        self.assertIsNotNone(r_est)
        self.assertEqual(r_est["id"], "102068")

        # 9. Tarefa inexistente retorna None (fail-fast)
        r_none = find_assignment_by_query("Física Quântica 99", sample_assignments)
        self.assertIsNone(r_none)

    def test_execute_solve_flow_aborts_on_course_mismatch(self):
        """Verifica se _execute_solve_flow aborta com alerta de segurança se houver incompatibilidade de matérias."""
        import asyncio
        from src.notifier.discord_bot import _execute_solve_flow

        mock_assignments = {
            "102068": {
                "id": "102068",
                "title": "Exercício 1 - Estatística Descritiva",
                "course": "2026_2 - FUNDAMENTOS DE ESTATÍSTICA E CIÊNCIA DE DADOS - TB1",
                "url": "https://virtual.ufmg.br/20262/mod/assign/view.php?id=102068",
                "activity_type": "assign"
            }
        }

        mock_send = AsyncMock()

        with patch("src.notifier.discord_bot._get_current_assignments", AsyncMock(return_value=mock_assignments)):
            success, msg = asyncio.run(_execute_solve_flow(
                send_func=mock_send,
                tarefa="102068",
                expected_course="2026_2 - INGLÊS INSTRUMENTAL I - METATURMA"
            ))

            self.assertFalse(success)
            self.assertIn("incompatibilidade", msg.lower())
            self.assertTrue(mock_send.called)
            sent_text = mock_send.call_args[0][0]
            self.assertIn("Incompatibilidade de Disciplina", sent_text)

    def test_bridge_runner_ignores_base_url_and_resolves_task(self):
        """Verifica se o BridgeRunner ignora URLs genéricas da nuvem e preserva o nome/id real."""
        import asyncio
        from src.scheduler.bridge_runner import BridgeRunner

        runner = BridgeRunner(render_url="https://mock-app.onrender.com")

        # Simula o payload que causou o incidente: assignment_url genérico + título de inglês
        task = {
            "task_id": "bridge_bug_test",
            "action": "solve_task",
            "assignment_id": "custom_12345",
            "assignment_url": "https://virtual.ufmg.br/20262",  # URL genérica perigosa
            "title": "Unidade 7 :: Aula 3 - Inglês Instrumental I",
            "course": "2026_2 - INGLÊS INSTRUMENTAL I - METATURMA",
            "channel_id": "123",
            "structured_answers": {
                "instrucoes": None,
                "modo": "resolver",
                "tarefa": "Unidade 7 :: Aula 3 - Inglês Instrumental I"
            }
        }

        with patch("src.notifier.discord_bot._execute_solve_flow", new_callable=AsyncMock) as mock_solve:
            mock_solve.return_value = (True, "Resolução concluída com sucesso")

            success, msg = asyncio.run(runner._execute_task(task))

            self.assertTrue(success)
            self.assertTrue(mock_solve.called)
            call_kwargs = mock_solve.call_args.kwargs
            self.assertNotEqual(call_kwargs["tarefa"], "https://virtual.ufmg.br/20262")
            self.assertIn("Aula 3", call_kwargs["tarefa"])
            self.assertIn("INGLÊS", call_kwargs["expected_course"].upper())


if __name__ == "__main__":
    unittest.main()

