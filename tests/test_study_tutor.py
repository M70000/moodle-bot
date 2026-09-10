"""Testes unitários para o módulo de Estudo Ativo, Tutor Acadêmico, Flashcards e Simulado."""

import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from config.settings import settings
from src.notifier.discord_bot import (
    FlashcardsCarouselView,
    InteractiveQuizSessionView,
    bot,
    get_study_target_channel,
)
from src.solver.study_tutor import (
    StudyTutor,
    clean_json_text,
    get_course_study_materials,
    normalize_str,
    resolve_course_materials_dir,
)


class TestStudyTutor(unittest.TestCase):

    def test_clean_json_text(self):
        # 1. Com bloco markdown ```json ... ```
        raw_md = "Aqui está o JSON:\n```json\n[{\"front\": \"Q1\", \"back\": \"A1\"}]\n```\nEspero que ajude!"
        cleaned = clean_json_text(raw_md)
        self.assertEqual(cleaned, '[{"front": "Q1", "back": "A1"}]')

        # 2. Com texto antes e depois sem code fence
        raw_text = "Comentários iniciais... [{\"id\": 1}] texto final."
        cleaned2 = clean_json_text(raw_text)
        self.assertEqual(cleaned2, '[{"id": 1}]')

    def test_answer_question_mocked(self):
        mock_solver = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Segundo o [Slide 14, Aula 03], o método nodal aplica a LKC a cada nó essencial..."
        mock_solver._generate_with_fallback = AsyncMock(return_value=(mock_response, "gemini-3.8-flash"))

        tutor = StudyTutor(solver=mock_solver)
        result = asyncio.run(tutor.answer_question("Cálculo 2", "Como funciona a regra da cadeia?"))

        self.assertIn("answer", result)
        self.assertIn("Slide 14", result["answer"])
        self.assertEqual(result["model_used"], "gemini-3.8-flash")
        self.assertEqual(result["discipline"], "Cálculo 2")
        self.assertTrue(mock_solver._generate_with_fallback.called)

    def test_generate_flashcards_and_anki_file(self):
        mock_solver = MagicMock()
        cards_json = """[
            {
                "front": "O que afirma o Teorema Fundamental do Cálculo?",
                "back": "A derivada da integral de uma função contínua é a própria função.",
                "explanation": "Cuidado com os limites de integração variáveis.",
                "source": "Slide 22, Aula 05"
            },
            {
                "front": "Qual a integral de sec(x)?",
                "back": "ln|sec(x) + tg(x)| + C",
                "explanation": "Multiplica-se numerador e denominador por (sec x + tg x).",
                "source": "Apostila Unidade 1"
            }
        ]"""
        mock_response = MagicMock()
        mock_response.text = cards_json
        mock_solver._generate_with_fallback = AsyncMock(return_value=(mock_response, "gemini-3.7-flash"))

        tutor = StudyTutor(solver=mock_solver)
        res = asyncio.run(tutor.generate_flashcards("Cálculo 2", topic="Integrais", count=2))

        self.assertEqual(len(res["cards"]), 2)
        self.assertEqual(res["cards"][0]["front"], "O que afirma o Teorema Fundamental do Cálculo?")
        self.assertIn("A derivada", res["cards"][0]["back"])

        # Valida arquivo Anki gerado
        anki_path = Path(res["anki_file_path"])
        self.assertTrue(anki_path.exists())
        content = anki_path.read_text(encoding="utf-8")
        self.assertIn("#separator:tab", content)
        self.assertIn("#html:true", content)
        self.assertIn("Teorema Fundamental", content)
        self.assertIn("ln|sec(x)", content)

    def test_generate_quiz_structure(self):
        mock_solver = MagicMock()
        quiz_json = """[
            {
                "id": 1,
                "question": "Qual a derivada de f(x) = x^3?",
                "options": {
                    "A": "3x",
                    "B": "3x^2",
                    "C": "x^2",
                    "D": "6x"
                },
                "correct_option": "B",
                "explanation": "Pela regra do tombo, d/dx(x^n) = n*x^(n-1).",
                "reference": "Slide 4, Aula 01"
            }
        ]"""
        mock_response = MagicMock()
        mock_response.text = quiz_json
        mock_solver._generate_with_fallback = AsyncMock(return_value=(mock_response, "gemini-3.5-flash"))

        tutor = StudyTutor(solver=mock_solver)
        res = asyncio.run(tutor.generate_quiz("Cálculo 2", num_questions=1))

        self.assertEqual(len(res["questions"]), 1)
        q1 = res["questions"][0]
        self.assertEqual(q1["correct_option"], "B")
        self.assertEqual(q1["options"]["B"], "3x^2")
        self.assertIn("regra do tombo", q1["explanation"])

    def test_flashcards_carousel_view_navigation(self):
        cards = [
            {"front": "Q1", "back": "A1", "explanation": "E1", "source": "S1"},
            {"front": "Q2", "back": "A2", "explanation": "E2", "source": "S2"},
        ]
        view = FlashcardsCarouselView(cards=cards, discipline="Cálculo", topic="Derivadas", requester="Aluno")

        # 1. Estado inicial
        self.assertEqual(view.current_idx, 0)
        self.assertFalse(view.is_flipped)
        self.assertTrue(view.btn_prev.disabled)
        self.assertFalse(view.btn_next.disabled)

        embed = view.build_embed()
        self.assertIn("Q1", embed.fields[0].value)
        self.assertIn("Resposta Oculta", embed.fields[1].name)

        # 2. Simula clique em revelar resposta
        mock_interaction = MagicMock()
        mock_interaction.response.edit_message = AsyncMock()
        asyncio.run(view.btn_flip.callback(mock_interaction))
        self.assertTrue(view.is_flipped)
        self.assertEqual(view.btn_flip.label, "Ocultar Resposta")

        embed_flipped = view.build_embed()
        self.assertIn("A1", embed_flipped.fields[1].value)

        # 3. Simula avançar para o próximo card
        asyncio.run(view.btn_next.callback(mock_interaction))
        self.assertEqual(view.current_idx, 1)
        self.assertFalse(view.is_flipped)
        self.assertFalse(view.btn_prev.disabled)
        self.assertTrue(view.btn_next.disabled)

    def test_interactive_quiz_session_view_flow(self):
        questions = [
            {
                "id": 1,
                "question": "Quanto é 2 + 2?",
                "options": {"A": "3", "B": "4", "C": "5", "D": "6"},
                "correct_option": "B",
                "explanation": "2+2=4.",
                "reference": "Slide 1"
            },
            {
                "id": 2,
                "question": "Quanto é 3 * 3?",
                "options": {"A": "6", "B": "8", "C": "9", "D": "12"},
                "correct_option": "C",
                "explanation": "3*3=9.",
                "reference": "Slide 2"
            }
        ]

        view = InteractiveQuizSessionView(
            questions=questions,
            discipline="Matemática",
            topic="Aritmética",
            requester="Aluno",
            requester_id=12345
        )

        mock_interaction = MagicMock()
        mock_interaction.user.id = 12345
        mock_interaction.response.edit_message = AsyncMock()

        # 1. Questão 1: Estudante clica na alternativa correta 'B'
        asyncio.run(view.btn_opt_b.callback(mock_interaction))
        self.assertTrue(view.is_answered)
        self.assertEqual(view.score, 1)
        self.assertEqual(view.user_answers[0], "B")
        self.assertFalse(view.btn_next_action.disabled)

        embed_q1 = view.build_question_embed()
        self.assertIn("Você Acertou", embed_q1.fields[0].name)

        # 2. Avança para a Questão 2
        asyncio.run(view.btn_next_action.callback(mock_interaction))
        self.assertEqual(view.current_idx, 1)
        self.assertFalse(view.is_answered)

        # 3. Questão 2: Estudante clica na alternativa incorreta 'A' (pegadinha)
        asyncio.run(view.btn_opt_a.callback(mock_interaction))
        self.assertTrue(view.is_answered)
        self.assertEqual(view.score, 1)  # Placar permanece 1
        embed_q2 = view.build_question_embed()
        self.assertIn("Atenção à Pegadinha", embed_q2.fields[0].name)

        # 4. Finaliza o simulado
        asyncio.run(view.btn_next_action.callback(mock_interaction))
        res_embed = view.build_results_embed()
        self.assertIn("1 de 2 questões corretas", res_embed.description)
        self.assertIn("50%", res_embed.description)

    def test_get_study_target_channel_routing(self):
        # 1. Quando DISCORD_STUDY_CHANNEL_ID é 0
        with patch.object(settings, "DISCORD_STUDY_CHANNEL_ID", 0):
            mock_ctx = MagicMock()
            mock_ctx.channel.id = 999
            target, redirected = get_study_target_channel(mock_ctx)
            self.assertEqual(target.id, 999)
            self.assertFalse(redirected)

        # 2. Quando DISCORD_STUDY_CHANNEL_ID é configurado (>0) e chamado de outro canal
        with patch.object(settings, "DISCORD_STUDY_CHANNEL_ID", 8888):
            mock_target_ch = MagicMock()
            mock_target_ch.id = 8888
            with patch.object(bot, "get_channel", return_value=mock_target_ch):
                mock_ctx = MagicMock()
                mock_ctx.channel.id = 1111  # Canal diferente
                target, redirected = get_study_target_channel(mock_ctx)
                self.assertEqual(target.id, 8888)
                self.assertTrue(redirected)

        # 3. Quando chamado diretamente de dentro do canal de estudos
        with patch.object(settings, "DISCORD_STUDY_CHANNEL_ID", 8888):
            mock_target_ch = MagicMock()
            mock_target_ch.id = 8888
            with patch.object(bot, "get_channel", return_value=mock_target_ch):
                mock_ctx = MagicMock()
                mock_ctx.channel.id = 8888  # Próprio canal
                target, redirected = get_study_target_channel(mock_ctx)
                self.assertEqual(target.id, 8888)
                self.assertFalse(redirected)


if __name__ == "__main__":
    unittest.main()
