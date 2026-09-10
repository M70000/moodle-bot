import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from config.settings import settings
from src.solver.gemini_solver import GeminiSolver


class TestGeminiSolverFallback(unittest.IsolatedAsyncioTestCase):
    """Testes para o motor de fallback hierárquico e timeouts do GeminiSolver."""

    def setUp(self):
        self.solver = GeminiSolver(api_key="test_api_key")
        self.solver.timeout_seconds = 0.1
        self.solver.fallback_timeout_seconds = 0.05
        self.solver.fallback_delay_seconds = 0.01

    def test_initialization_includes_safety_models(self):
        """Garante que modelos ultrarrápidos de segurança estejam na hierarquia."""
        self.assertIn("gemini-3.5-flash-lite", self.solver.model_hierarchy)
        self.assertIn("gemini-flash-lite-latest", self.solver.model_hierarchy)
        self.assertEqual(self.solver.timeout_seconds, 0.1)

    async def test_fallback_advances_on_timeout(self):
        """Simula tempo limite no modelo principal e valida transição imediata para o fallback."""
        logged_messages = []

        async def mock_on_log(msg):
            logged_messages.append(msg)

        # Mock client.aio.models.generate_content
        mock_response = MagicMock()
        mock_response.text = "Resposta de sucesso do fallback"

        async def fake_generate_content(model, contents, config=None):
            if model == self.solver.model_hierarchy[0]:
                # Demora mais que 0.1s para disparar TimeoutError
                await asyncio.sleep(0.3)
                return MagicMock(text="Tarde demais")
            return mock_response

        with patch.object(self.solver.client.aio.models, "generate_content", side_effect=fake_generate_content):
            resp, used_model = await self.solver._generate_with_fallback(
                contents=["teste"],
                temperature=0.1,
                on_log=mock_on_log
            )

        self.assertEqual(resp.text, "Resposta de sucesso do fallback")
        self.assertEqual(used_model, self.solver.model_hierarchy[1])

        # Verifica se o log registrou o timeout e a transição
        any_timeout_log = any("esgotado" in m.lower() or "limite" in m.lower() for m in logged_messages)
        self.assertTrue(any_timeout_log, f"Nenhum log de timeout encontrado: {logged_messages}")

    async def test_fallback_advances_on_503_high_demand(self):
        """Simula erro 503 de alta demanda e valida chaveamento imediato para o próximo modelo."""
        logged_messages = []

        async def mock_on_log(msg):
            logged_messages.append(msg)

        mock_response = MagicMock()
        mock_response.text = "Sucesso no modelo 3"

        call_counts = {}

        async def fake_generate_503(model, contents, config=None):
            call_counts[model] = call_counts.get(model, 0) + 1
            if model == self.solver.model_hierarchy[0]:
                raise Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")
            elif model == self.solver.model_hierarchy[1]:
                raise Exception("503 UNAVAILABLE. High demand.")
            return mock_response

        with patch.object(self.solver.client.aio.models, "generate_content", side_effect=fake_generate_503):
            resp, used_model = await self.solver._generate_with_fallback(
                contents=["teste 503"],
                temperature=0.1,
                on_log=mock_on_log
            )

        self.assertEqual(resp.text, "Sucesso no modelo 3")
        self.assertEqual(used_model, self.solver.model_hierarchy[2])

        # Verifica se o log informou sobre alta demanda
        any_503_log = any("alta demanda" in m.lower() or "503" in m.lower() for m in logged_messages)
        self.assertTrue(any_503_log, f"Nenhum log de 503 encontrado: {logged_messages}")

    async def test_all_models_fail_raises_runtime_error(self):
        """Garante que RuntimeError seja levantado se absolutamente todos os modelos falharem."""
        async def fake_always_fail(model, contents, config=None):
            raise Exception("Erro geral de conexão")

        with patch.object(self.solver.client.aio.models, "generate_content", side_effect=fake_always_fail):
            with self.assertRaises(RuntimeError) as ctx:
                await self.solver._generate_with_fallback(contents=["falha total"])

        self.assertIn("Todos os modelos da hierarquia falharam", str(ctx.exception))

    def test_collect_context_files_does_not_crawl_materials_directory(self):
        """Valida que _collect_context_files NÃO busca arquivos de storage/materials automaticamente."""
        from src.scraper.moodle_scraper import Assignment, CourseMaterial
        from pathlib import Path

        assign = Assignment(
            id="101",
            course_id="1",
            course_name="2026_2 - INGLÊS INSTRUMENTAL I - METATURMA",
            title="Unidade 3 :: Aula 2",
            url="http://example.com",
            attachments=[]
        )
        collected = self.solver._collect_context_files(assign)
        self.assertEqual(collected, [], "Nenhum arquivo deve ser coletado automaticamente se a tarefa não tiver anexos diretos!")

    def test_get_course_materials_for_task(self):
        """Valida que get_course_materials_for_task localiza corretamente os arquivos salvos em storage/materials."""
        from src.notifier.discord_bot import get_course_materials_for_task

        materials = get_course_materials_for_task("45706")
        self.assertTrue(len(materials) > 0, "Deve encontrar materiais para a tarefa 45706 de Inglês Instrumental")
        self.assertTrue(any("RESPOSTAS" in m.name for m in materials))


if __name__ == "__main__":
    unittest.main()
