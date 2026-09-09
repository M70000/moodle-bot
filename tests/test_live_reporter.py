import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.notifier.discord_bot import DiscordLiveReporter
from src.scraper.moodle_submitter import _emit_log


class TestDiscordLiveReporter(unittest.IsolatedAsyncioTestCase):
    async def test_reporter_formatting_and_rich_strip(self):
        mock_msg = MagicMock()
        mock_msg.edit = AsyncMock()

        reporter = DiscordLiveReporter(mock_msg, "⏳ Test Header")
        await reporter.log("[bold cyan]Iniciando tarefa teste[/bold cyan]")

        self.assertEqual(len(reporter.logs), 1)
        self.assertIn("Iniciando tarefa teste", reporter.logs[0])
        self.assertNotIn("[bold cyan]", reporter.logs[0])
        mock_msg.edit.assert_awaited()
        call_args = mock_msg.edit.call_args[1]
        self.assertIn("```bash", call_args["content"])
        self.assertIn("Iniciando tarefa teste", call_args["content"])

    async def test_reporter_buffer_limit(self):
        mock_msg = MagicMock()
        mock_msg.edit = AsyncMock()

        reporter = DiscordLiveReporter(mock_msg, "⏳ Test Header")
        for i in range(20):
            await reporter.log(f"Linha de log {i+1}")

        # Buffer máximo deve ser de 12 linhas
        self.assertEqual(len(reporter.logs), 12)
        self.assertIn("Linha de log 20", reporter.logs[-1])
        self.assertNotIn("Linha de log 1", reporter.logs[0])

    async def test_reporter_finish_clean_replacement(self):
        mock_msg = MagicMock()
        mock_msg.edit = AsyncMock()

        reporter = DiscordLiveReporter(mock_msg, "⏳ Test Header")
        await reporter.log("Executando passo 1...")
        await reporter.finish("🎉 Concluído com sucesso!")

        self.assertTrue(reporter._is_closed)
        call_args = mock_msg.edit.call_args[1]
        self.assertEqual(call_args["content"], "🎉 Concluído com sucesso!")
        self.assertNotIn("```bash", call_args["content"])

    async def test_emit_log_sync_and_async(self):
        # Callback síncrono
        sync_called = []
        def sync_cb(msg):
            sync_called.append(msg)

        await _emit_log(sync_cb, "msg 1")
        self.assertEqual(sync_called, ["msg 1"])

        # Callback assíncrono
        async_called = []
        async def async_cb(msg):
            async_called.append(msg)

        await _emit_log(async_cb, "msg 2")
        self.assertEqual(async_called, ["msg 2"])

        # Callback None não gera erro
        await _emit_log(None, "msg 3")


if __name__ == "__main__":
    unittest.main()
