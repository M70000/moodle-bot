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

    async def test_handle_http_request_materials(self):
        """Testa se a rota GET e POST /api/bridge/materials responde corretamente."""
        # 1. POST material
        mock_reader_post = AsyncMock()
        payload = json.dumps({
            "course": "Física 2",
            "filename": "resumo_p1.pdf",
            "attachment_url": "https://cdn.discordapp.com/resumo.pdf"
        }).encode("utf-8")
        mock_reader_post.readline.side_effect = [
            b"POST /api/bridge/materials HTTP/1.1\r\n",
            f"Content-Length: {len(payload)}\r\n".encode("utf-8"),
            b"\r\n",
        ]
        mock_reader_post.readexactly.return_value = payload

        mock_writer_post = MagicMock()
        mock_writer_post.write = MagicMock()
        mock_writer_post.drain = AsyncMock()
        mock_writer_post.close = MagicMock()
        mock_writer_post.wait_closed = AsyncMock()

        await handle_http_request(mock_reader_post, mock_writer_post)
        resp_post = mock_writer_post.write.call_args[0][0].decode("utf-8")
        self.assertIn("HTTP/1.1 200 OK", resp_post)
        self.assertIn('"ok": true', resp_post)

        # 2. GET materials
        mock_reader_get = AsyncMock()
        mock_reader_get.readline.side_effect = [
            b"GET /api/bridge/materials HTTP/1.1\r\n",
            b"\r\n",
        ]
        mock_writer_get = MagicMock()
        mock_writer_get.write = MagicMock()
        mock_writer_get.drain = AsyncMock()
        mock_writer_get.close = MagicMock()
        mock_writer_get.wait_closed = AsyncMock()

        await handle_http_request(mock_reader_get, mock_writer_get)
        resp_get = mock_writer_get.write.call_args[0][0].decode("utf-8")
        self.assertIn("HTTP/1.1 200 OK", resp_get)
        self.assertIn("resumo_p1.pdf", resp_get)



if __name__ == "__main__":
    unittest.main()
