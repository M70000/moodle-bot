"""Inicializador da Interface Gráfica de Configuração do Moodle Bot."""

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.ui.server import create_server


def find_free_port(start_port: int = 5055, max_attempts: int = 20) -> int:
    """Encontra uma porta TCP livre a partir de start_port."""
    for p in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start_port


def find_browser_app_executable() -> str:
    """Busca o executável do Edge ou Chrome no Windows para abrir em modo App limpo."""
    if sys.platform != "win32":
        return ""

    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidates.extend([
            os.path.join(local_app_data, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe"),
        ])

    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def main():
    parser = argparse.ArgumentParser(description="Moodle Bot - Interface Gráfica de Configuração")
    parser.add_argument("--port", type=int, default=0, help="Porta local do servidor (padrão: 5055 ou livre)")
    parser.add_argument("--browser", action="store_true", help="Forçar abertura no navegador padrão em vez de modo app")
    parser.add_argument("--no-open", action="store_true", help="Não abrir janela automaticamente")
    args = parser.parse_args()

    port = args.port or find_free_port(5055)
    url = f"http://127.0.0.1:{port}"

    try:
        httpd = create_server(host="127.0.0.1", port=port)
    except Exception as err:
        print(f"Erro ao iniciar servidor na porta {port}: {err}")
        sys.exit(1)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    print("\n" + "=" * 60)
    print("  Moodle AI Assistant - Painel de Configuração")
    print("=" * 60)
    print(f"✔ Servidor rodando em: {url}")
    print("✔ Pressione Ctrl+C para encerrar o painel.")
    print("=" * 60 + "\n")

    if not args.no_open:
        app_exe = find_browser_app_executable()
        opened = False
        if app_exe and not args.browser:
            try:
                # Abre em modo janela de aplicativo nativo (--app)
                subprocess.Popen([
                    app_exe,
                    f"--app={url}",
                    "--window-size=1040,820",
                    "--window-position=120,80"
                ])
                opened = True
            except Exception as e:
                print(f"Nota ao abrir modo app: {e}, abrindo no navegador padrão...")

        if not opened:
            webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nEncerrando servidor de configuração...")
        httpd.shutdown()
        httpd.server_close()
        print("Finalizado com sucesso.")


if __name__ == "__main__":
    main()
