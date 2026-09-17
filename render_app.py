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

# Garante que os prints apareçam em tempo real nos logs do Render sem buffer
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

import discord

from config.settings import settings
from src.notifier.bridge_manager import cloud_bridge
from src.notifier.discord_bot import bot

# Estado de rastreamento da conexão do Discord
bot_runtime_state = {
    "status": "iniciando",
    "token_configured": False,
    "last_error": None,
    "connected_at": None,
}


async def notify_discord_completion(task: dict):

    """Atualiza o embed e envia comprovante no canal do Discord após execução pelo desktop."""
    try:
        channel_id = int(task.get("channel_id") or 0)
        message_id = int(task.get("message_id") or 0)
        if not channel_id:
            return

        channel = bot.get_channel(channel_id)
        if not channel:
            channel = await bot.fetch_channel(channel_id)
        if not channel:
            return

        success = task.get("success", False)
        result_msg = task.get("result_message", "")
        title = task.get("title", "Atividade")
        action = task.get("action", "")

        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                if msg:
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
            except Exception as msg_err:
                print(f"[Render Bridge] Nota ao atualizar mensagem original {message_id}: {msg_err}")

        # Se for solve_task / redo_task sem message_id:
        if action in ("solve_task", "redo_task") and not message_id:
            if not success:
                await channel.send(
                    f"⚠️ **Falha no processamento de '{title}' pelo Desktop Runner:**\n{result_msg}"
                )
            return

        # Se for ação de integração com Notion:
        if action.startswith("notion_"):
            if success:
                await channel.send(
                    f"✔ **Notion & Agenda:**\n{result_msg}"
                )
            else:
                await channel.send(
                    f"⚠️ **Falha na integração com Notion:**\n{result_msg}"
                )
            return

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
            token_val = (os.environ.get("DISCORD_BOT_TOKEN") or getattr(settings, "DISCORD_BOT_TOKEN", "") or "").strip()
            response_dict = {
                "status": "online",
                "service": "Moodle AI Assistant - Discord Bot Central & Cloud Bridge",
                "timestamp": datetime.now().isoformat(),
                "discord_ready": bot.is_ready(),
                "user": str(bot.user) if bot.is_ready() else None,
                "guilds": len(bot.guilds) if bot.is_ready() else 0,
                "bot_status": bot_runtime_state["status"],
                "token_configured": bool(token_val),
                "token_preview": f"{token_val[:6]}***" if token_val else "AUSENTE",
                "last_error": bot_runtime_state["last_error"],
            }


        elif path == "/api/bridge/pending":
            channel_id = query_params.get("channel_id", [""])[0] or None
            tasks = await cloud_bridge.get_pending_tasks(channel_id=channel_id)
            response_dict = {"tasks": tasks, "count": len(tasks)}

        elif path in ("/api/bridge/courses", "/api/bridge/sync"):
            channel_id = query_params.get("channel_id", [""])[0] or None
            if method == "POST":
                # Desktop publica a lista de disciplinas, catálogo de tarefas e materiais
                try:
                    data = json.loads(body_bytes.decode("utf-8"))
                    courses = data.get("courses")
                    assignments = data.get("assignments")
                    custom_materials = data.get("custom_materials")
                    cid = data.get("channel_id") or channel_id
                    lms_provider = data.get("lms_provider")
                    await cloud_bridge.publish_state(
                        courses=courses,
                        assignments=assignments,
                        custom_materials=custom_materials,
                        channel_id=cid,
                        lms_provider=lms_provider
                    )
                    c_count = len(courses) if courses is not None else 0
                    a_count = len(assignments) if assignments is not None else 0
                    m_count = len(custom_materials) if custom_materials is not None else 0
                    response_dict = {
                        "ok": True,
                        "channel_id": cid,
                        "lms_provider": lms_provider or cloud_bridge.get_published_provider(channel_id=cid),
                        "courses_count": c_count,
                        "assignments_count": a_count,
                        "materials_count": m_count,
                    }
                    print(f"[Bridge] Desktop ({cid or 'global'}) sincronizou {c_count} disciplinas, {a_count} tarefas e {m_count} materiais (LMS: {lms_provider or 'moodle'}).")
                except Exception as e:
                    status_code = "400 Bad Request"
                    response_dict = {"error": str(e)}
            else:
                # Autocomplete (relay mode) lê a lista publicada pelo desktop para aquele canal
                courses = await cloud_bridge.get_published_courses(channel_id=channel_id)
                online = await cloud_bridge.is_desktop_online(channel_id=channel_id)
                provider = cloud_bridge.get_published_provider(channel_id=channel_id)
                response_dict = {
                    "courses": courses,
                    "channel_id": channel_id,
                    "lms_provider": provider,
                    "desktop_online": online,
                    "count": len(courses)
                }

        elif path == "/api/bridge/assignments":
            channel_id = query_params.get("channel_id", [""])[0] or None
            assignments = await cloud_bridge.get_published_assignments(channel_id=channel_id)
            online = await cloud_bridge.is_desktop_online(channel_id=channel_id)
            provider = cloud_bridge.get_published_provider(channel_id=channel_id)
            response_dict = {
                "assignments": assignments,
                "channel_id": channel_id,
                "lms_provider": provider,
                "desktop_online": online,
                "count": len(assignments)
            }

        elif path == "/api/bridge/materials":
            channel_id = query_params.get("channel_id", [""])[0] or None
            if method == "POST":
                try:
                    data = json.loads(body_bytes.decode("utf-8"))
                    cid = data.get("channel_id") if isinstance(data, dict) else channel_id
                    if isinstance(data, list):
                        for item in data:
                            item_cid = item.get("channel_id", cid) if isinstance(item, dict) else cid
                            await cloud_bridge.register_material(item, channel_id=item_cid)
                    elif isinstance(data, dict):
                        await cloud_bridge.register_material(data, channel_id=cid)
                    mats = await cloud_bridge.get_custom_materials(channel_id=cid)
                    response_dict = {"ok": True, "count": len(mats)}
                except Exception as e:
                    status_code = "400 Bad Request"
                    response_dict = {"error": str(e)}
            else:
                mats = await cloud_bridge.get_custom_materials(channel_id=channel_id)
                response_dict = {"materials": mats, "count": len(mats)}

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


async def render_keep_alive():
    """Realiza auto-ping periódico no endpoint /healthz para evitar hibernação no plano Free do Render."""
    await asyncio.sleep(60)
    external_url = os.environ.get("RENDER_EXTERNAL_URL") or getattr(settings, "RENDER_URL", "")
    if not external_url:
        print("[KeepAlive] Nota: RENDER_EXTERNAL_URL não definida. Para manter o bot online 24/7 com o PC desligado, configure um monitor gratuito no UptimeRobot ou cron-job.org.")
        return

    url = f"{external_url.rstrip('/')}/healthz"
    print(f"[KeepAlive] Rotina de auto-ping ativada para: {url} (a cada 10 minutos)")

    import httpx
    while True:
        try:
            await asyncio.sleep(600)
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers={"User-Agent": "RenderAutoKeepAlive/1.0"})
                print(f"[KeepAlive] Ping executado em {url} -> Status {resp.status_code}")
        except asyncio.CancelledError:
            break
        except Exception as err:
            print(f"[KeepAlive] Nota ao executar ping: {err}")


async def run_discord_supervisor():
    """Gerencia a conexão com o Discord Gateway, capturando erros de token e intents."""
    token = (os.environ.get("DISCORD_BOT_TOKEN") or getattr(settings, "DISCORD_BOT_TOKEN", "") or "").strip()
    if not token:
        bot_runtime_state["status"] = "missing_token"
        bot_runtime_state["last_error"] = "DISCORD_BOT_TOKEN não foi configurado nas Environment Variables do Render."
        print("=" * 60, flush=True)
        print("❌ [Render] AVISO CRÍTICO: DISCORD_BOT_TOKEN NÃO ESTÁ CONFIGURADO!", flush=True)
        print("👉 Acesse o Dashboard do Render -> Seu Web Service -> Aba 'Environment'", flush=True)
        print("👉 Adicione a variável DISCORD_BOT_TOKEN com o token do seu bot.", flush=True)
        print("=" * 60, flush=True)
        return

    bot_runtime_state["token_configured"] = True
    bot_runtime_state["status"] = "connecting"
    print(f"🤖 [Render] Conectando ao Discord Gateway (Token: {token[:6]}***)...", flush=True)

    try:
        @bot.event
        async def on_connect():
            bot_runtime_state["status"] = "connected"
            print("✔ [Discord] Conexão estabelecida com o Discord Gateway!", flush=True)

        original_on_ready = bot.on_ready
        async def _wrapped_on_ready():
            bot_runtime_state["status"] = "ready"
            bot_runtime_state["connected_at"] = datetime.now().isoformat()
            print(f"🎉 [Discord] Bot online e pronto no Render como: {bot.user} (Servidores: {len(bot.guilds)})", flush=True)
            if original_on_ready:
                await original_on_ready()

        bot.on_ready = _wrapped_on_ready
        await bot.start(token)

    except discord.LoginFailure as lf:
        bot_runtime_state["status"] = "login_failed"
        bot_runtime_state["last_error"] = f"Token do Discord inválido ou expirado: {lf}"
        print("=" * 60, flush=True)
        print(f"❌ [Discord] ERRO DE AUTENTICAÇÃO: O token do Discord foi rejeitado!", flush=True)
        print(f"👉 Detalhes: {lf}", flush=True)
        print("👉 Verifique o valor de DISCORD_BOT_TOKEN na aba Environment do Render.", flush=True)
        print("=" * 60, flush=True)
    except discord.PrivilegedIntentsRequired as pi:
        bot_runtime_state["status"] = "intents_required"
        bot_runtime_state["last_error"] = f"Privileged Intents necessárias não habilitadas: {pi}"
        print("=" * 60, flush=True)
        print("❌ [Discord] ERRO DE INTENTS PRIVILEGIADAS:", flush=True)
        print("👉 Acesse discord.com/developers/applications -> Seu Bot -> Seção 'Bot'", flush=True)
        print("👉 Ative as 3 opções em 'Privileged Gateway Intents':", flush=True)
        print("   - Presence Intent", flush=True)
        print("   - Server Members Intent", flush=True)
        print("   - Message Content Intent", flush=True)
        print("=" * 60, flush=True)
    except Exception as exc:
        bot_runtime_state["status"] = "error"
        bot_runtime_state["last_error"] = str(exc)
        print(f"❌ [Discord] Erro inesperado na execução do bot: {exc}", flush=True)
        import traceback
        traceback.print_exc()


async def main():
    port = int(os.environ.get("PORT", 10000))
    host = "0.0.0.0"

    print("=" * 60, flush=True)
    print("  🚀 LumiBot - Render Cloud Service & Bridge Hub", flush=True)
    print(f"  Porta HTTP: {port} | Host: {host}", flush=True)
    print("=" * 60, flush=True)

    server = await asyncio.start_server(handle_http_request, host, port)
    print(f"✔ Servidor de Health Check & Bridge ativo em http://{host}:{port}/healthz", flush=True)

    async with server:
        tasks = [
            server.serve_forever(),
            render_keep_alive(),
            run_discord_supervisor(),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Encerrado pelo usuário.", flush=True)

