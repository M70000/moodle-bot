"""Entrypoint para execução contínua 24/7 do Bot no Render (Web Service Free).

Inicia um servidor HTTP assíncrono leve na porta $PORT para atender aos health checks
do Render e pings de keep-alive, executando o Moodle Discord Bot no mesmo event loop.
"""

import asyncio
import json
import os
import sys
from datetime import datetime

from config.settings import settings
from src.notifier.discord_bot import bot, run_bot


async def handle_http_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Trata requisições HTTP básicas (GET /, GET /healthz) para o health check do Render."""
    try:
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            await writer.wait_closed()
            return

        # Lê os headers até a linha em branco
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break

        body_data = {
            "status": "online",
            "service": "Moodle AI Assistant - Discord Bot Central",
            "timestamp": datetime.now().isoformat(),
            "discord_ready": bot.is_ready(),
            "user": str(bot.user) if bot.is_ready() else None,
            "guilds": len(bot.guilds) if bot.is_ready() else 0,
        }
        body_bytes = json.dumps(body_data, indent=2).encode("utf-8")

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body_bytes)).encode("utf-8") + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body_bytes
        )
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    port = int(os.environ.get("PORT", 10000))
    host = "0.0.0.0"

    print("=" * 60)
    print("  🚀 Moodle Bot - Render Cloud Service")
    print(f"  Porta HTTP: {port} | Host: {host}")
    print("=" * 60)

    server = await asyncio.start_server(handle_http_request, host, port)
    print(f"✔ Servidor de Health Check ativo em http://{host}:{port}/healthz")

    if not settings.DISCORD_BOT_TOKEN:
        print("⚠ AVISO: DISCORD_BOT_TOKEN não foi configurado nas Environment Variables do Render!")
    else:
        print("🤖 Conectando ao Discord Gateway...")

    async with server:
        tasks = [server.serve_forever()]
        if settings.DISCORD_BOT_TOKEN:
            tasks.append(run_bot())
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Encerrado pelo usuário.")
