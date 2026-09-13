"""Gerenciador da Bandeja do Sistema (System Tray) para o Moodle AI Assistant.

Permite que o assistente seja minimizado para a área de notificação do Windows,
removendo a janela da barra de tarefas para eliminar poluição visual enquanto
permanece em execução contínua em segundo plano.
"""

import ctypes
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
from rich.console import Console

console = Console()

SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
GA_ROOT = 2
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040


class SystemTrayManager:
    """Gerencia o ícone na bandeja do sistema e o comportamento de minimizar."""

    def __init__(
        self,
        on_exit_callback: Optional[Callable[[], None]] = None,
        config_url: str = "http://127.0.0.1:5055",
    ):
        self.on_exit_callback = on_exit_callback
        self.config_url = config_url
        self.icon = None
        self._running = False
        self._is_hidden = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._target_hwnd = 0

        self.root_dir = Path(__file__).resolve().parent.parent.parent
        self.logo_path = self.root_dir / "assets" / "logo.png"
        self.ico_path = self.root_dir / "assets" / "app.ico"

    def _find_window_hwnd(self) -> int:
        """Localiza o identificador de janela (HWND) do console ou terminal."""
        if sys.platform != "win32":
            return 0

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 1. Tenta obter diretamente o HWND do console
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            root = user32.GetAncestor(hwnd, GA_ROOT)
            return root if root else hwnd

        # 2. Tenta buscar por título de janela conhecido
        known_titles = [
            "Moodle AI Assistant - Assistente em Execucao",
            "Moodle AI Assistant",
        ]
        for title in known_titles:
            h = user32.FindWindowW(None, title)
            if h:
                root = user32.GetAncestor(h, GA_ROOT)
                return root if root else h

        # 3. Busca por enumeração de janelas pertencentes ao processo atual
        pid = os.getpid()
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)

        def enum_cb(h, _):
            if user32.IsWindowVisible(h):
                proc_id = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(h, ctypes.byref(proc_id))
                if proc_id.value == pid:
                    found.append(h)
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        except Exception:
            pass

        return found[0] if found else 0

    def _apply_window_icon(self, hwnd: int):
        """Aplica o Brasão de Minas Gerais como ícone da janela do Windows."""
        if sys.platform != "win32" or not hwnd or not self.ico_path.exists():
            return
        try:
            user32 = ctypes.windll.user32
            hicon = user32.LoadImageW(
                0,
                str(self.ico_path.resolve()),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
        except Exception:
            pass

    def restore_window(self, icon=None, item=None):
        """Restaura a janela para o estado normal e traz para o primeiro plano."""
        if sys.platform != "win32":
            return

        hwnd = self._target_hwnd or self._find_window_hwnd()
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            self._is_hidden = False

    def hide_window(self, icon=None, item=None):
        """Oculta a janela completamente da tela e da barra de tarefas."""
        if sys.platform != "win32":
            return

        hwnd = self._target_hwnd or self._find_window_hwnd()
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, SW_HIDE)
            self._is_hidden = True

    def open_config_panel(self, icon=None, item=None):
        """Abre o painel de controle e configurações no navegador."""
        try:
            webbrowser.open(self.config_url)
        except Exception as e:
            console.print(f"[yellow]Aviso ao abrir navegador: {e}[/yellow]")

    def _on_exit(self, icon=None, item=None):
        """Finaliza o assistente a partir do menu da bandeja."""
        self.stop()
        if self.on_exit_callback:
            try:
                self.on_exit_callback()
            except Exception:
                pass
        else:
            # Encerra o processo de forma limpa
            os._exit(0)

    def _monitor_window_loop(self):
        """Monitora continuamente o estado da janela para ocultar quando minimizada."""
        if sys.platform != "win32":
            return

        user32 = ctypes.windll.user32

        while self._running:
            time.sleep(0.3)
            try:
                if not self._target_hwnd:
                    self._target_hwnd = self._find_window_hwnd()
                    if self._target_hwnd:
                        self._apply_window_icon(self._target_hwnd)

                if self._target_hwnd and not self._is_hidden:
                    # Verifica se o usuário clicou em minimizar ('_')
                    if user32.IsIconic(self._target_hwnd):
                        # Janela foi minimizada -> oculta da barra de tarefas
                        user32.ShowWindow(self._target_hwnd, SW_HIDE)
                        self._is_hidden = True
            except Exception:
                pass

    def start(self) -> bool:
        """Inicia o ícone na bandeja e o monitor de minimização."""
        if sys.platform != "win32":
            return False

        try:
            import pystray
            from pystray import Menu, MenuItem as item
        except ImportError:
            console.print("[yellow]Aviso: Biblioteca pystray não disponível. System tray desabilitado.[/yellow]")
            return False

        try:
            # Carrega a imagem do brasão
            if self.logo_path.exists():
                img = Image.open(self.logo_path).convert("RGBA")
                tray_img = img.resize((64, 64), Image.Resampling.LANCZOS)
            else:
                tray_img = Image.new("RGBA", (64, 64), (198, 40, 40, 255))

            menu = Menu(
                item("Abrir Janela", self.restore_window, default=True),
                item("Minimizar para a Bandeja", self.hide_window),
                item("Abrir Painel de Configurações", self.open_config_panel),
                Menu.SEPARATOR,
                item("Status: Ativo e Monitorando", lambda i, it: None, enabled=False),
                Menu.SEPARATOR,
                item("Encerrar Assistente", self._on_exit),
            )

            self.icon = pystray.Icon(
                "moodle_ai_assistant",
                tray_img,
                "Moodle AI Assistant (UFMG) - Ativo",
                menu,
            )

            self._running = True

            # Inicia o ícone da bandeja em thread desacoplada
            self.icon.run_detached()

            # Inicia o monitor de minimização
            self._target_hwnd = self._find_window_hwnd()
            if self._target_hwnd:
                self._apply_window_icon(self._target_hwnd)

            self._monitor_thread = threading.Thread(
                target=self._monitor_window_loop,
                name="TrayWindowMonitor",
                daemon=True,
            )
            self._monitor_thread.start()

            return True
        except Exception as err:
            console.print(f"[yellow]Aviso ao inicializar bandeja do sistema: {err}[/yellow]")
            return False

    def stop(self):
        """Encerra o ícone da bandeja e restaura a janela caso estivesse oculta."""
        self._running = False
        if sys.platform == "win32" and self._is_hidden and self._target_hwnd:
            try:
                ctypes.windll.user32.ShowWindow(self._target_hwnd, SW_RESTORE)
            except Exception:
                pass

        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
