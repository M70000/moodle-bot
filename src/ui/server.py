"""Servidor HTTP embutido para a Interface Gráfica de Configuração do Moodle Bot."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

# Raiz do projeto (c:\moodle-bot)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"


def parse_env_file(path: Path) -> Dict[str, str]:
    """Lê um arquivo .env retornando um dicionário chave-valor."""
    config: Dict[str, str] = {}
    if not path.exists():
        return config
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Remove aspas se houver
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                config[key] = val
    except Exception as e:
        print(f"[UI Server] Erro ao ler {path}: {e}")
    return config


def get_current_config() -> Dict[str, Any]:
    """Obtém as configurações atuais combinando defaults do .env.example com o .env real."""
    defaults = parse_env_file(ENV_EXAMPLE_PATH)
    current = parse_env_file(ENV_PATH)

    # Defaults de fallback caso .env.example não tenha algo
    base_defaults = {
        "MOODLE_BASE_URL": "https://virtual.ufmg.br",
        "DISCORD_BOT_TOKEN": "",
        "DISCORD_CHANNEL_ID": "0",
        "DISCORD_CONTENT_CHANNEL_ID": "0",
        "DISCORD_ANNOUNCEMENTS_CHANNEL_ID": "0",
        "DISCORD_QUEUE_CHANNEL_ID": "0",
        "DISCORD_STUDY_CHANNEL_ID": "0",
        "RENDER_URL": "",
        "GEMINI_API_KEY": "",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "GEMINI_FALLBACK_MODEL_1": "gemini-3.7-flash",
        "GEMINI_FALLBACK_MODEL_2": "gemini-3.5-flash",
        "GEMINI_FALLBACK_MODEL_3": "gemini-3.5-flash-lite",
        "GEMINI_TIMEOUT_SECONDS": "90",
        "GEMINI_FALLBACK_TIMEOUT_SECONDS": "60",
        "GEMINI_FALLBACK_DELAY_SECONDS": "2.0",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": "claude-haiku-4-5",
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MODEL": "deepseek-chat",
        "CHECK_INTERVAL_MINUTES": "30",
        "EMERGENCY_SUBMIT_ENABLED": "false",
        "STORAGE_COOKIES_PATH": "storage/cookies/session.json",
        "STORAGE_MATERIALS_DIR": "storage/materials",
        "STORAGE_SUBMISSIONS_DIR": "storage/submissions",
        "HEADLESS_LOGIN": "false",
        "LOGIN_TIMEOUT_SECONDS": "300",
        "NOTION_API_KEY": "",
        "NOTION_PAGE_ID": "17db4e452b43449a9ca266065840f909",
        "NOTION_TASKS_DATABASE_ID": "00e5c698-5139-4b4c-9cac-db04bfc22c4b",
        "NOTION_COURSES_DATABASE_ID": "751117de-c4d2-468c-9b46-571c036969b1",
        "NOTION_DAILY_CHECKLIST_BLOCK_ID": "25cd128a-26fe-49ac-8ab0-a895f1e0858d",
        "NOTION_WEEKLY_SCHEDULE_TABLE_ID": "2a9222dd-474a-4c40-9b96-a548f2c9ec11",
    }

    merged = dict(base_defaults)
    merged.update(defaults)
    merged.update(current)

    return {
        "config": merged,
        "env_exists": ENV_PATH.exists(),
        "env_path": str(ENV_PATH),
    }


def save_config_to_env(new_values: Dict[str, Any]) -> None:
    """Salva os valores atualizados formatados e organizados no .env."""
    lines = [
        "# ===================================================================",
        "# Moodle AI Assistant (UFMG) - Arquivo de Configuração de Ambiente",
        f"# Atualizado via Interface Gráfica em: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "# ===================================================================",
        "",
        "# -------------------------------------------------------------------",
        "# 1. Plataforma Moodle & UFMG Virtual",
        "# -------------------------------------------------------------------",
        f"MOODLE_BASE_URL={new_values.get('MOODLE_BASE_URL', 'https://virtual.ufmg.br')}",
        f"HEADLESS_LOGIN={str(new_values.get('HEADLESS_LOGIN', 'false')).lower()}",
        f"LOGIN_TIMEOUT_SECONDS={new_values.get('LOGIN_TIMEOUT_SECONDS', '300')}",
        "",
        "# -------------------------------------------------------------------",
        "# 2. Discord Bot - Notificações e Revisão",
        "# -------------------------------------------------------------------",
        f"DISCORD_BOT_TOKEN={new_values.get('DISCORD_BOT_TOKEN', '').strip()}",
        f"DISCORD_CHANNEL_ID={new_values.get('DISCORD_CHANNEL_ID', '0').strip() or '0'}",
        f"DISCORD_CONTENT_CHANNEL_ID={new_values.get('DISCORD_CONTENT_CHANNEL_ID', '0').strip() or '0'}",
        f"DISCORD_ANNOUNCEMENTS_CHANNEL_ID={new_values.get('DISCORD_ANNOUNCEMENTS_CHANNEL_ID', '0').strip() or '0'}",
        f"DISCORD_QUEUE_CHANNEL_ID={new_values.get('DISCORD_QUEUE_CHANNEL_ID', '0').strip() or '0'}",
        f"DISCORD_STUDY_CHANNEL_ID={new_values.get('DISCORD_STUDY_CHANNEL_ID', '0').strip() or '0'}",
        f"RENDER_URL={new_values.get('RENDER_URL', '').strip()}",
        "",
        "# -------------------------------------------------------------------",
        "# 3. Provedores de IA (Gemini, Claude, DeepSeek - BYOK)",
        "# -------------------------------------------------------------------",
        f"GEMINI_API_KEY={new_values.get('GEMINI_API_KEY', '').strip()}",
        f"GEMINI_MODEL={new_values.get('GEMINI_MODEL', 'gemini-3.8-flash').strip()}",
        f"GEMINI_FALLBACK_MODEL_1={new_values.get('GEMINI_FALLBACK_MODEL_1', 'gemini-3.7-flash').strip()}",
        f"GEMINI_FALLBACK_MODEL_2={new_values.get('GEMINI_FALLBACK_MODEL_2', 'gemini-3.5-flash').strip()}",
        f"GEMINI_FALLBACK_MODEL_3={new_values.get('GEMINI_FALLBACK_MODEL_3', 'gemini-3.5-flash-lite').strip()}",
        f"GEMINI_TIMEOUT_SECONDS={new_values.get('GEMINI_TIMEOUT_SECONDS', '90')}",
        f"GEMINI_FALLBACK_TIMEOUT_SECONDS={new_values.get('GEMINI_FALLBACK_TIMEOUT_SECONDS', '60')}",
        f"GEMINI_FALLBACK_DELAY_SECONDS={new_values.get('GEMINI_FALLBACK_DELAY_SECONDS', '2.0')}",
        "",
        "# Anthropic Claude (Opcional - BYOK alternativo)",
        f"ANTHROPIC_API_KEY={new_values.get('ANTHROPIC_API_KEY', '').strip()}",
        f"ANTHROPIC_MODEL={new_values.get('ANTHROPIC_MODEL', 'claude-haiku-4-5').strip()}",
        "",
        "# DeepSeek (Opcional - BYOK alternativo)",
        f"DEEPSEEK_API_KEY={new_values.get('DEEPSEEK_API_KEY', '').strip()}",
        f"DEEPSEEK_MODEL={new_values.get('DEEPSEEK_MODEL', 'deepseek-chat').strip()}",
        "",
        "# -------------------------------------------------------------------",
        "# 4. Monitoramento e Agendamento",
        "# -------------------------------------------------------------------",
        f"CHECK_INTERVAL_MINUTES={new_values.get('CHECK_INTERVAL_MINUTES', '30')}",
        f"EMERGENCY_SUBMIT_ENABLED={str(new_values.get('EMERGENCY_SUBMIT_ENABLED', 'false')).lower()}",
        "",
        "# -------------------------------------------------------------------",
        "# 5. Armazenamento e Diretórios",
        "# -------------------------------------------------------------------",
        f"STORAGE_COOKIES_PATH={new_values.get('STORAGE_COOKIES_PATH', 'storage/cookies/session.json')}",
        f"STORAGE_MATERIALS_DIR={new_values.get('STORAGE_MATERIALS_DIR', 'storage/materials')}",
        f"STORAGE_SUBMISSIONS_DIR={new_values.get('STORAGE_SUBMISSIONS_DIR', 'storage/submissions')}",
        "",
        "# -------------------------------------------------------------------",
        "# 6. Integração Notion (Central de Estudos e Tarefas)",
        "# -------------------------------------------------------------------",
        f"NOTION_API_KEY={new_values.get('NOTION_API_KEY', '').strip()}",
        f"NOTION_PAGE_ID={new_values.get('NOTION_PAGE_ID', '17db4e452b43449a9ca266065840f909').strip()}",
        f"NOTION_TASKS_DATABASE_ID={new_values.get('NOTION_TASKS_DATABASE_ID', '00e5c698-5139-4b4c-9cac-db04bfc22c4b').strip()}",
        f"NOTION_COURSES_DATABASE_ID={new_values.get('NOTION_COURSES_DATABASE_ID', '751117de-c4d2-468c-9b46-571c036969b1').strip()}",
        f"NOTION_DAILY_CHECKLIST_BLOCK_ID={new_values.get('NOTION_DAILY_CHECKLIST_BLOCK_ID', '25cd128a-26fe-49ac-8ab0-a895f1e0858d').strip()}",
        f"NOTION_WEEKLY_SCHEDULE_TABLE_ID={new_values.get('NOTION_WEEKLY_SCHEDULE_TABLE_ID', '2a9222dd-474a-4c40-9b96-a548f2c9ec11').strip()}",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")


def get_system_status() -> Dict[str, Any]:
    """Retorna o status geral de autenticação, cookies e diretórios."""
    cookies_file = PROJECT_ROOT / "storage" / "cookies" / "session.json"
    materials_dir = PROJECT_ROOT / "storage" / "materials"
    submissions_dir = PROJECT_ROOT / "storage" / "submissions"

    has_session = cookies_file.exists() and cookies_file.stat().st_size > 10
    session_date = None
    if has_session:
        mtime = cookies_file.stat().st_mtime
        session_date = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y às %H:%M")

    materials_count = 0
    if materials_dir.exists():
        materials_count = len(list(materials_dir.rglob("*.*")))

    submissions_count = 0
    if submissions_dir.exists():
        submissions_count = len(list(submissions_dir.rglob("*.pdf")))

    return {
        "session_exists": has_session,
        "session_date": session_date,
        "materials_count": materials_count,
        "submissions_count": submissions_count,
    }


def test_moodle_connection(url: str) -> Dict[str, Any]:
    """Verifica se a URL do Moodle está online e respondendo."""
    if not url:
        return {"ok": False, "error": "URL do Moodle não informada."}
    target = url.rstrip("/")
    start_time = time.time()
    try:
        req = urllib.request.Request(
            target,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MoodleBotChecker/1.0"}
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            elapsed = round((time.time() - start_time) * 1000)
            return {
                "ok": True,
                "status_code": resp.status,
                "elapsed_ms": elapsed,
                "message": f"Moodle respondeu com sucesso ({resp.status} OK em {elapsed}ms)."
            }
    except urllib.error.HTTPError as e:
        elapsed = round((time.time() - start_time) * 1000)
        return {
            "ok": True,
            "status_code": e.code,
            "elapsed_ms": elapsed,
            "message": f"Servidor Moodle acessível (HTTP {e.code} em {elapsed}ms)."
        }
    except Exception as e:
        return {"ok": False, "error": f"Falha ao alcançar o Moodle: {str(e)}"}


def test_gemini_connection(api_key: str, model_name: str) -> Dict[str, Any]:
    """Testa a chave e o modelo do Google Gemini usando o SDK google-genai."""
    if not api_key:
        return {"ok": False, "error": "Chave de API do Gemini não informada."}
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        start_time = time.time()
        model = model_name or "gemini-3.8-flash"
        response = client.models.generate_content(
            model=model,
            contents="Responda estritamente com uma palavra: 'OK'."
        )
        elapsed = round((time.time() - start_time) * 1000)
        text = getattr(response, "text", "").strip()
        return {
            "ok": True,
            "model": model,
            "elapsed_ms": elapsed,
            "response": text or "OK",
            "message": f"Google Gemini ({model}) validado com sucesso ({elapsed}ms)!"
        }
    except Exception as e:
        err_msg = str(e)
        if "API_KEY_INVALID" in err_msg or "400" in err_msg:
            return {"ok": False, "error": "Chave de API inválida no Google AI Studio."}
        if "RESOURCE_EXHAUSTED" in err_msg or "429" in err_msg:
            return {"ok": False, "error": "Cota do Gemini excedida temporariamente (Resource Exhausted)."}
        if "NOT_FOUND" in err_msg or "404" in err_msg:
            return {"ok": False, "error": f"Modelo '{model_name}' não encontrado para sua chave de API."}
        return {"ok": False, "error": f"Erro na API Gemini: {err_msg[:200]}"}


def test_discord_connection(bot_token: str, channel_id: Optional[str] = None) -> Dict[str, Any]:
    """Testa o token do Bot do Discord e opcionalmente o acesso ao canal."""
    if not bot_token:
        return {"ok": False, "error": "Token do Bot do Discord não informado."}
    try:
        headers = {
            "Authorization": f"Bot {bot_token.strip()}",
            "User-Agent": "MoodleAssistantBot (https://moodle.bot, 1.0)",
        }
        # 1. Valida o bot
        req = urllib.request.Request("https://discord.com/api/v10/users/@me", headers=headers)
        with urllib.request.urlopen(req, timeout=7.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            bot_name = data.get("username", "Bot")
            bot_tag = f"{bot_name}#{data.get('discriminator', '0')}" if data.get('discriminator') != '0' else bot_name
            bot_id = data.get("id")

        channel_info = None
        # 2. Se informou canal, tenta inspecionar
        if channel_id and channel_id.strip() not in ["0", ""]:
            try:
                ch_req = urllib.request.Request(f"https://discord.com/api/v10/channels/{channel_id.strip()}", headers=headers)
                with urllib.request.urlopen(ch_req, timeout=5.0) as ch_resp:
                    ch_data = json.loads(ch_resp.read().decode("utf-8"))
                    channel_info = {
                        "name": ch_data.get("name"),
                        "type": ch_data.get("type"),
                        "ok": True,
                    }
            except Exception as ch_err:
                channel_info = {"ok": False, "error": "Canal não encontrado ou bot sem permissão nele."}

        return {
            "ok": True,
            "bot_name": bot_tag,
            "bot_id": bot_id,
            "channel_info": channel_info,
            "message": f"Bot '{bot_tag}' autenticado com sucesso no Discord!"
        }
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "error": "Token de bot inválido (HTTP 401 Unauthorized)."}
        return {"ok": False, "error": f"Erro do Discord HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"Falha na conexão com Discord: {str(e)}"}




class ConfigAPIHandler(SimpleHTTPRequestHandler):
    """Handler HTTP para servir a interface estática e os endpoints da API REST."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def _send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        url_path = self.path.split("?")[0]

        if url_path == "/api/config":
            self._send_json(get_current_config())
            return

        if url_path == "/api/status":
            self._send_json(get_system_status())
            return

        # Rota padrão para SPA
        if url_path in ["", "/"]:
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self):
        url_path = self.path.split("?")[0]
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        if url_path == "/api/config":
            try:
                save_config_to_env(payload)
                self._send_json({"ok": True, "message": "Configurações salvas no arquivo .env com sucesso!"})
            except Exception as e:
                self._send_json({"ok": False, "error": f"Erro ao salvar arquivo .env: {str(e)}"}, status=500)
            return

        if url_path == "/api/test-moodle":
            url = payload.get("url", "")
            res = test_moodle_connection(url)
            self._send_json(res)
            return

        if url_path == "/api/test-gemini":
            api_key = payload.get("api_key", "")
            model = payload.get("model", "")
            res = test_gemini_connection(api_key, model)
            self._send_json(res)
            return

        if url_path == "/api/test-discord":
            token = payload.get("token", "")
            channel_id = payload.get("channel_id", "")
            res = test_discord_connection(token, channel_id)
            self._send_json(res)
            return


        if url_path == "/api/login-moodle":

            try:
                # Inicia o moodle_auth em nova janela de console para o usuário interagir
                venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
                python_bin = str(venv_python) if venv_python.exists() else sys.executable
                creation_flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
                subprocess.Popen(
                    [python_bin, "-m", "src.auth.moodle_auth"],
                    cwd=str(PROJECT_ROOT),
                    creationflags=creation_flags
                )
                self._send_json({"ok": True, "message": "Janela do navegador para login no MinhaUFMG aberta!"})
            except Exception as e:
                self._send_json({"ok": False, "error": f"Erro ao iniciar login: {str(e)}"}, status=500)
            return

        if url_path == "/api/open-folder":
            folder_type = payload.get("folder", "root")
            target = PROJECT_ROOT
            if folder_type == "materials":
                target = PROJECT_ROOT / "storage" / "materials"
            elif folder_type == "submissions":
                target = PROJECT_ROOT / "storage" / "submissions"
            elif folder_type == "cookies":
                target = PROJECT_ROOT / "storage" / "cookies"

            target.mkdir(parents=True, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(str(target))
                else:
                    subprocess.Popen(["xdg-open", str(target)])
                self._send_json({"ok": True, "message": f"Pasta aberta: {target.name}"})
            except Exception as e:
                self._send_json({"ok": False, "error": f"Não foi possível abrir a pasta: {e}"})
            return

        self._send_json({"error": "Endpoint não encontrado"}, status=404)

    def log_message(self, format, *args):
        # Silencia logs repetitivos de arquivos estáticos no console
        pass


def create_server(host: str = "127.0.0.1", port: int = 5055) -> ThreadingHTTPServer:
    """Cria e retorna a instância do ThreadingHTTPServer configurado."""
    return ThreadingHTTPServer((host, port), ConfigAPIHandler)
