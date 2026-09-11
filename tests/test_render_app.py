"""Testes unitários para o entrypoint do Render (render_app.py)."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from render_app import handle_http_request


class TestRenderApp(unittest.IsolatedAsyncioTestCase):
    """Valida o servidor HTTP de healthcheck do Render."""

    async def test_handle_http_request_healthz(self):
        """Testa se a rota /healthz responde com HTTP 200 OK e JSON com status."""
        mock_reader = AsyncMock()
        # Simula request GET /healthz HTTP/1.1
        mock_reader.readline.side_effect = [
            b"GET /healthz HTTP/1.1\r\n",
            b"Host: 127.0.0.1\r\n",
            b"\r\n",  # fim dos headers
        ]

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        await handle_http_request(mock_reader, mock_writer)

        self.assertTrue(mock_writer.write.called)
        response_bytes = mock_writer.write.call_args[0][0]
        response_str = response_bytes.decode("utf-8")

        self.assertIn("HTTP/1.1 200 OK", response_str)
        self.assertIn("Content-Type: application/json", response_str)
        self.assertIn('"status": "online"', response_str)
        self.assertIn("Moodle AI Assistant", response_str)
        mock_writer.close.assert_called()


if __name__ == "__main__":
    unittest.main()
