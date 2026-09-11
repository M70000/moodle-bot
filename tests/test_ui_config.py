"""Testes unitários para o módulo de Interface Gráfica de Configuração (src.ui)."""

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from src.ui.server import (
    create_server,
    get_current_config,
    get_system_status,
    parse_env_file,
    save_config_to_env,
    test_discord_connection as check_discord_connection,
    test_gemini_connection as check_gemini_connection,
    test_moodle_connection as check_moodle_connection,
)



class TestUIConfig(unittest.TestCase):
    def test_parse_env_file(self):
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8") as f:
            f.write("# Comentário\n")
            f.write("KEY_A=valor_a\n")
            f.write('KEY_B="valor_b com aspas"\n')
            f.write("KEY_C='valor_c simples'\n")
            f.write("\n")
            f.write("# Outro comentário\n")
            f.write("KEY_D=123\n")
            tmp_path = Path(f.name)

        try:
            parsed = parse_env_file(tmp_path)
            self.assertEqual(parsed.get("KEY_A"), "valor_a")
            self.assertEqual(parsed.get("KEY_B"), "valor_b com aspas")
            self.assertEqual(parsed.get("KEY_C"), "valor_c simples")
            self.assertEqual(parsed.get("KEY_D"), "123")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_get_current_config(self):
        cfg_data = get_current_config()
        self.assertIn("config", cfg_data)
        self.assertIn("env_exists", cfg_data)
        cfg = cfg_data["config"]
        self.assertIn("MOODLE_BASE_URL", cfg)
        self.assertIn("DISCORD_BOT_TOKEN", cfg)
        self.assertIn("GEMINI_MODEL", cfg)

    def test_test_moodle_connection_empty(self):
        res = check_moodle_connection("")
        self.assertFalse(res["ok"])
        self.assertIn("não informada", res["error"])

    def test_test_gemini_connection_empty(self):
        res = check_gemini_connection("", "gemini-3.8-flash")
        self.assertFalse(res["ok"])
        self.assertIn("não informada", res["error"])

    def test_test_discord_connection_empty(self):
        res = check_discord_connection("")
        self.assertFalse(res["ok"])
        self.assertIn("não informado", res["error"])

    def test_get_system_status(self):
        status = get_system_status()
        self.assertIn("session_exists", status)
        self.assertIn("materials_count", status)
        self.assertIn("submissions_count", status)

    def test_http_server_endpoints(self):
        # Inicia servidor em porta livre
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        httpd = create_server(host="127.0.0.1", port=port)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        base_url = f"http://127.0.0.1:{port}"

        try:
            # 1. GET /api/config
            with urllib.request.urlopen(f"{base_url}/api/config", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("config", data)

            # 2. GET /api/status
            with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("session_exists", data)

            # 3. GET /index.html (Servindo arquivo estático)
            with urllib.request.urlopen(f"{base_url}/index.html", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertIn("Moodle AI", body)

            # 4. POST /api/test-gemini
            req = urllib.request.Request(
                f"{base_url}/api/test-gemini",
                data=json.dumps({"api_key": "", "model": "gemini-3.8-flash"}).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(data["ok"])

        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
