"""Testes unitários para a seleção explícita de provedor de IA e cadeia de fallbacks."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from src.solver.ai_solver import (
    AISolver,
    get_active_provider,
    get_fallback_chain,
    normalize_provider,
)


class TestAIMultiProvider(unittest.TestCase):

    def test_normalize_provider(self):
        self.assertEqual(normalize_provider("gemini"), "gemini")
        self.assertEqual(normalize_provider("Google-Gemini"), "gemini")
        self.assertEqual(normalize_provider("claude"), "anthropic")
        self.assertEqual(normalize_provider("Anthropic"), "anthropic")
        self.assertEqual(normalize_provider("deepseek"), "deepseek")
        self.assertEqual(normalize_provider("Deep-Seek"), "deepseek")
        self.assertIsNone(normalize_provider("none"))
        self.assertIsNone(normalize_provider("nenhum"))
        self.assertIsNone(normalize_provider("desativado"))
        self.assertIsNone(normalize_provider(""))
        self.assertIsNone(normalize_provider(None))

    def test_get_fallback_chain_default(self):
        with patch.object(settings, "AI_PROVIDER", "gemini"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_1", "gemini"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_2", "deepseek"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_3", "none"):
            chain = get_fallback_chain()
            self.assertEqual(chain, ["gemini", "deepseek"])

    def test_get_fallback_chain_claude_primary(self):
        with patch.object(settings, "AI_PROVIDER", "claude"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_1", "gemini"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_2", "deepseek"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_3", "none"):
            chain = get_fallback_chain()
            self.assertEqual(chain, ["anthropic", "gemini", "deepseek"])

    def test_get_active_provider_formatting(self):
        with patch.object(settings, "AI_PROVIDER", "claude"), \
             patch.object(settings, "ANTHROPIC_MODEL", "claude-haiku-4-5"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_1", "gemini"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_2", "deepseek"):
            active = get_active_provider()
            self.assertIn("Anthropic Claude", active)
            self.assertIn("claude-haiku-4-5", active)
            self.assertIn("Fallback: Gemini → DeepSeek", active)

    @patch("src.solver.ai_solver._call_claude", new_callable=AsyncMock)
    @patch("src.solver.ai_solver._call_deepseek", new_callable=AsyncMock)
    def test_generate_text_fallback_on_error(self, mock_deepseek, mock_claude):
        import asyncio

        # Claude configurado como primário, mas falha (ex: rate limit 429)
        mock_claude.side_effect = RuntimeError("Rate limit 429")
        # DeepSeek configurado como fallback e tem sucesso
        mock_deepseek.return_value = ("Resposta do DeepSeek", "deepseek-chat")

        with patch.object(settings, "AI_PROVIDER", "claude"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_1", "deepseek"), \
             patch.object(settings, "AI_FALLBACK_PROVIDER_2", "none"), \
             patch.object(settings, "ANTHROPIC_API_KEY", "sk-ant-test"), \
             patch.object(settings, "DEEPSEEK_API_KEY", "sk-deepseek-test"):
            solver = AISolver()
            text, model = asyncio.run(solver.generate_text("System", "User prompt"))

            self.assertEqual(text, "Resposta do DeepSeek")
            self.assertEqual(model, "deepseek-chat")
            self.assertTrue(mock_claude.called)
            self.assertTrue(mock_deepseek.called)

    def test_deepseek_flash_call_parameters(self):
        """Verifica se _call_deepseek passa model='deepseek-flash', base_url, thinking mode e CoT."""
        import asyncio
        from src.solver.ai_solver import _call_deepseek

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_message = MagicMock()
        mock_message.content = '{"status": "success"}'
        mock_message.reasoning_content = "Passo 1: Analisar os requisitos. Passo 2: Gerar JSON."
        mock_message.tool_calls = None
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]

        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}), \
             patch.object(settings, "DEEPSEEK_API_KEY", "sk-deepseek-test"), \
             patch.object(settings, "DEEPSEEK_MODEL", "deepseek-flash"), \
             patch.object(settings, "DEEPSEEK_BASE_URL", "https://api.deepseek.com"), \
             patch.object(settings, "DEEPSEEK_THINKING_MODE", True), \
             patch.object(settings, "DEEPSEEK_REASONING_EFFORT", "high"):

            text, model = asyncio.run(_call_deepseek(
                system_instruction="Instrução do sistema",
                user_message="Pergunta do usuário",
                json_output=True,
            ))

            self.assertEqual(text, '{"status": "success"}')
            self.assertEqual(model, "deepseek-flash")

            # Verifica se create foi chamado com os parâmetros exigidos pelo DeepSeek Flash
            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            self.assertEqual(call_kwargs["model"], "deepseek-flash")
            self.assertEqual(call_kwargs["response_format"], {"type": "json_object"})
            self.assertEqual(call_kwargs["extra_body"], {"thinking": {"type": "enabled"}})
            self.assertEqual(call_kwargs["reasoning_effort"], "high")

    def test_deepseek_flash_vision_multimodal(self):
        """Verifica se _call_deepseek formata imagens inline em base64 data URL para o modo Flash."""
        import asyncio
        from src.solver.ai_solver import _call_deepseek

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_message = MagicMock()
        mock_message.content = "Diagrama analisado com sucesso."
        mock_message.reasoning_content = None
        mock_message.tool_calls = None
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]

        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI = MagicMock(return_value=mock_client)

        fake_image_bytes = b"\x89PNG\r\n\x1a\nfake_png_data"

        with patch.dict("sys.modules", {"openai": mock_openai}), \
             patch.object(settings, "DEEPSEEK_API_KEY", "sk-deepseek-test"), \
             patch.object(settings, "DEEPSEEK_MODEL", "deepseek-flash"):

            text, model = asyncio.run(_call_deepseek(
                system_instruction="Analise a imagem",
                user_message="O que há nesta figura?",
                images=[fake_image_bytes],
            ))

            self.assertEqual(text, "Diagrama analisado com sucesso.")
            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            messages = call_kwargs["messages"]
            user_msg = [m for m in messages if m["role"] == "user"][0]
            self.assertIsInstance(user_msg["content"], list)
            self.assertEqual(user_msg["content"][0]["type"], "text")
            self.assertEqual(user_msg["content"][1]["type"], "image_url")
            self.assertTrue(user_msg["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_aisolver_generate_json(self):
        """Testa o método generate_json com parsing automático de objeto JSON."""
        import asyncio

        with patch.object(settings, "AI_PROVIDER", "deepseek"), \
             patch.object(settings, "DEEPSEEK_API_KEY", "sk-test"), \
             patch("src.solver.ai_solver._call_deepseek", new_callable=AsyncMock) as mock_ds:

            mock_ds.return_value = ('{"question": "Qual a capital?", "answer": "Brasília"}', "deepseek-flash")

            solver = AISolver()
            parsed, used_model = asyncio.run(solver.generate_json("System prompt", "User prompt"))

            self.assertEqual(parsed["question"], "Qual a capital?")
            self.assertEqual(parsed["answer"], "Brasília")
            self.assertEqual(used_model, "deepseek-flash")

    def test_aisolver_apply_revision_with_deepseek_flash(self):
        """Testa o fluxo de apply_revision fazendo nova chamada via DeepSeek Flash e atualizando arquivos."""
        import asyncio
        from pathlib import Path
        from src.solver.gemini_solver import SolutionDraft

        old_draft = SolutionDraft(
            assignment_id="999",
            assignment_title="Algoritmos - Trabalho 1",
            course_name="Algoritmos",
            summary="Versão antiga",
            full_markdown="### Questão 1\nComplexidade O(n²)",
            output_path=Path("storage/test_old.md"),
            activity_type="assign",
            auto_triggered=False
        )

        with patch.object(settings, "AI_PROVIDER", "deepseek"), \
             patch.object(settings, "DEEPSEEK_API_KEY", "sk-test"), \
             patch("src.solver.ai_solver._call_deepseek", new_callable=AsyncMock) as mock_ds, \
             patch("src.solver.docx_generator.AcademicDocxGenerator.generate_docx", return_value=True):

            mock_ds.return_value = ("### Questão 1\nComplexidade otimizada para O(n log n)", "deepseek-flash")

            solver = AISolver()
            new_draft = asyncio.run(solver.apply_revision(
                draft=old_draft,
                revision_instructions="Otimize o algoritmo da questão 1 para O(n log n)."
            ))

            self.assertTrue(mock_ds.called)
            call_system, call_user = mock_ds.call_args[0][:2]
            self.assertIn("Complexidade O(n²)", call_user)
            self.assertIn("Otimize o algoritmo", call_user)
            self.assertIn("Questão 1", new_draft.full_markdown)
            self.assertIn("O(n log n)", new_draft.full_markdown)
            self.assertEqual(new_draft.used_model, "deepseek-flash")
            self.assertEqual(new_draft.activity_type, "assign")


if __name__ == "__main__":
    unittest.main()
