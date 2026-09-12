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


if __name__ == "__main__":
    unittest.main()
