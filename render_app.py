"""Entrypoint para execução contínua 24/7 do Bot no Render (Web Service Free).

Inicia um servidor HTTP assíncrono leve na porta $PORT para atender aos health checks
do Render e pings de keep-alive, servindo também como Hub Central da Ponte Nuvem
para despachar ações de submissão do Discord para os computadores locais dos alunos.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import discord

from config.settings import settings
from src.notifier.bridge_manager import cloud_bridge
from src.notifier.discord_bot import bot, run_bot


async def notify_discord_completion(task: dict):
    """Atualiza o embed e envia comprovante no canal do Discord após execução pelo desktop."""
    try:
        channel_id = int(task.get("channel_id") or 0)
        message_id = int(task.get("message_id") or 0)
        if not channel_id or not message_id:
            return

        channel = bot.get_channel(channel_id)
        if not channel:
            channel = await bot.fetch_channel(channel_id)
        if not channel:
            return

        msg = await channel.fetch_message(message_id)
        if not msg:
            return

        success = task.get("success", False)
        result_msg = task.get("result_message", "")
        title = task.get("title", "Atividade")

        embed = msg.embeds[0] if msg.embeds else None
        if embed:
            current_time = datetime.now().strftime("%H:%M:%S")
            if success:
                embed.color = discord.Color.green()
                embed.title = f"✅ Submetido com Sucesso: {title}"
                embed.set_footer(
                    text=f"Submetido no Moodle via Desktop Runner às {current_time}."
                )
            else:
                embed.color = discord.Color.red()
                embed.set_footer(
                    text=f"Falha na submissão via Desktop Runner: {result_msg[:100]}"
                )

        await msg.edit(embed=embed, view=None)

        if success:
            await channel.send(
                f"🎉 **Confirmação de Envio no Moodle:**\n"
                f"Atividade **{title}** submetida com sucesso pelo seu executor desktop!\n"
                f"{result_msg}"
            )
        else:
            await channel.send(
                f"⚠️ **Falha no envio da atividade {title}:**\n"
                f"{result_msg}"
            )
    except Exception as err:
        print(f"[Render Bridge] Erro ao atualizar Discord: {err}")


async def handle_http_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Trata requisições HTTP (Healthcheck e Ponte Nuvem)."""
    try:
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            await writer.wait_closed()
            return

        parts = request_line.decode("utf-8", errors="ignore").split()
        if len(parts) < 2:
            writer.close()
            await writer.wait_closed()
            return

        method, full_path = parts[0].upper(), parts[1]
        parsed_url = urlparse(full_path)
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)

        # Lê os headers
        content_length = 0
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            header_str = line.decode("utf-8", errors="ignore").lower()
            if header_str.startswith("content-length:"):
                try:
                    content_length = int(header_str.split(":", 1)[1].strip())
                except ValueError:
                    pass

        # Lê body se houver
        body_bytes = b""
        if content_length > 0:
            body_bytes = await reader.readexactly(content_length)

        # Roteamento
        status_code = "200 OK"
        response_dict = {}

        if path in ("/", "/healthz"):
            response_dict = {
                "status": "online",
                "service": "Moodle AI Assistant - Discord Bot Central & Cloud Bridge",
                "timestamp": datetime.now().isoformat(),
                "discord_ready": bot.is_ready(),
                "user": str(bot.user) if bot.is_ready() else None,
                "guilds": len(bot.guilds) if bot.is_ready() else 0,
            }

        elif path == "/api/bridge/pending":
            channel_id = query_params.get("channel_id", [""])[0] or None
            tasks = await cloud_bridge.get_pending_tasks(channel_id=channel_id)
            response_dict = {"tasks": tasks, "count": len(tasks)}

        elif path in ("/api/bridge/courses", "/api/bridge/sync"):
            if method == "POST":
                # Desktop publica a lista de disciplinas e catálogo de tarefas
                try:
                    data = json.loads(body_bytes.decode("utf-8"))
                    courses = data.get("courses")
                    assignments = data.get("assignments")
                    await cloud_bridge.publish_state(courses=courses, assignments=assignments)
                    c_count = len(courses) if courses is not None else 0
                    a_count = len(assignments) if assignments is not None else 0
                    response_dict = {
                        "ok": True,
                        "courses_count": c_count,
                        "assignments_count": a_count,
                    }
                    print(f"[Bridge] Desktop sincronizou {c_count} disciplinas e {a_count} tarefas.")
                except Exception as e:
                    status_code = "400 Bad Request"
                    response_dict = {"error": str(e)}
            else:
                # Autocomplete (relay mode) lê a lista publicada pelo desktop
                courses = await cloud_bridge.get_published_courses()
                online = await cloud_bridge.is_desktop_online()
                response_dict = {
                    "courses": courses,
                    "desktop_online": online,
                    "count": len(courses)
                }

        elif path == "/api/bridge/assignments":
            assignments = await cloud_bridge.get_published_assignments()
            online = await cloud_bridge.is_desktop_online()
            response_dict = {
                "assignments": assignments,
                "desktop_online": online,
                "count": len(assignments)
            }

        elif path == "/api/bridge/desktop-status":
            status = await cloud_bridge.desktop_status()
            response_dict = status

        elif path == "/api/bridge/claim" and method == "POST":
            try:
                data = json.loads(body_bytes.decode("utf-8"))
                task_id = data.get("task_id")
                if task_id:
                    await cloud_bridge.mark_in_progress(task_id)
                    response_dict = {"status": "claimed", "task_id": task_id}
                else:
                    status_code = "400 Bad Request"
                    response_dict = {"error": "Missing task_id"}
            except Exception as e:
                status_code = "400 Bad Request"
                response_dict = {"error": str(e)}

        elif path == "/api/bridge/complete" and method == "POST":
            try:
                data = json.loads(body_bytes.decode("utf-8"))
                task_id = data.get("task_id")
                success = bool(data.get("success", False))
                message = str(data.get("message", ""))
                task = await cloud_bridge.complete_task(task_id, success, message)
                if task:
                    # Notifica Discord de forma assíncrona
                    asyncio.create_task(notify_discord_completion(task))
                    response_dict = {"status": "completed", "task_id": task_id}
                else:
                    status_code = "404 Not Found"
                    response_dict = {"error": "Task not found"}
            except Exception as e:
                status_code = "400 Bad Request"
                response_dict = {"error": str(e)}
        else:
            status_code = "404 Not Found"
            response_dict = {"error": "Not Found"}

        resp_json = json.dumps(response_dict, indent=2).encode("utf-8")
        headers = (
            f"HTTP/1.1 {status_code}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(resp_json)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        response = headers + resp_json
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
    print("  🚀 Moodle Bot - Render Cloud Service & Bridge Hub")
    print(f"  Porta HTTP: {port} | Host: {host}")
    print("=" * 60)

    server = await asyncio.start_server(handle_http_request, host, port)
    print(f"✔ Servidor de Health Check & Bridge ativo em http://{host}:{port}/healthz")

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
