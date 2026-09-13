"""Gerenciador da Bandeja do Sistema (System Tray) para o Moodle AI Assistant.

Permite que o assistente seja minimizado para a área de notificação do Windows,
removendo a janela da barra de tarefas para eliminar poluição visual enquanto
permanece em execução contínua em segundo plano.
"""

import ctypes
from ctypes import wintypes
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
SW_SHOWNORMAL = 1
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
GA_ROOT = 2
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040

# Configuração de protótipos Win32 64-bit seguros
if sys.platform == "win32":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
    kernel32.SetConsoleTitleW.restype = wintypes.BOOL

    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetAncestor.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int


class SystemTrayManager:
    """Gerencia o ícone na bandeja do sistema e o comportamento de minimizar."""

    WINDOW_TITLE = "Moodle AI Assistant - Assistente em Execucao"

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
        self._target_hwnd: Optional[int] = None

        self.root_dir = Path(__file__).resolve().parent.parent.parent
        self.logo_path = self.root_dir / "assets" / "logo.png"
        self.ico_path = self.root_dir / "assets" / "app.ico"

    def _find_window_hwnd(self) -> int:
        """Localiza com precisão o identificador da janela visível (Windows Terminal ou Conhost)."""
        if sys.platform != "win32":
            return 0

        # Garante que o console possua o título padrão do assistente
        try:
            kernel32.SetConsoleTitleW(self.WINDOW_TITLE)
        except Exception:
            pass

        # 1. Busca direta pelo título exato configurado no iniciar.bat
        exact_h = user32.FindWindowW(None, self.WINDOW_TITLE)
        if exact_h and user32.IsWindow(exact_h):
            root = user32.GetAncestor(exact_h, GA_ROOT)
            return root if (root and user32.IsWindow(root)) else exact_h

        # 2. Busca por enumeração de janelas visíveis que contenham "Moodle AI Assistant"
        found_h = 0
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_cb(h, _):
            nonlocal found_h
            if user32.IsWindow(h) and user32.IsWindowVisible(h):
                length = user32.GetWindowTextLengthW(h)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(h, buf, length + 1)
                    title_str = buf.value.lower()
                    if "moodle ai assistant" in title_str:
                        found_h = h
                        return False
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
            if found_h and user32.IsWindow(found_h):
                root = user32.GetAncestor(found_h, GA_ROOT)
                return root if (root and user32.IsWindow(root)) else found_h
        except Exception:
            pass

        # 3. Fallback: GetConsoleWindow (conhost clássico)
        con_h = kernel32.GetConsoleWindow()
        if con_h and user32.IsWindow(con_h):
            root = user32.GetAncestor(con_h, GA_ROOT)
            return root if (root and user32.IsWindow(root)) else con_h

        return 0

    def _apply_window_icon(self, hwnd: int):
        """Aplica o Brasão de Minas Gerais como ícone da janela do Windows."""
        if sys.platform != "win32" or not hwnd or not user32.IsWindow(hwnd) or not self.ico_path.exists():
            return
        try:
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
            self._target_hwnd = hwnd
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.ShowWindow(hwnd, SW_SHOW)
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            self._is_hidden = False

    def hide_window(self, icon=None, item=None):
        """Oculta a janela completamente da tela e da barra de tarefas."""
        if sys.platform != "win32":
            return

        hwnd = self._target_hwnd or self._find_window_hwnd()
        if hwnd:
            self._target_hwnd = hwnd
            user32.ShowWindow(hwnd, SW_HIDE)
            self._is_hidden = True

    def toggle_window(self, icon=None, item=None):
        """Alterna o estado da janela ao clicar no ícone da bandeja."""
        if sys.platform != "win32":
            return

        hwnd = self._target_hwnd or self._find_window_hwnd()
        if not hwnd:
            return

        self._target_hwnd = hwnd
        if self._is_hidden:
            self.restore_window(icon, item)
        else:
            self.hide_window(icon, item)


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
            os._exit(0)

    def _monitor_window_loop(self):
        """Monitora continuamente o estado da janela para ocultar quando minimizada."""
        if sys.platform != "win32":
            return

        while self._running:
            time.sleep(0.2)
            try:
                # Se ainda não encontramos o HWND ou a janela foi alterada
                if not self._target_hwnd or not user32.IsWindow(self._target_hwnd):
                    self._target_hwnd = self._find_window_hwnd()
                    if self._target_hwnd and user32.IsWindow(self._target_hwnd):
                        self._apply_window_icon(self._target_hwnd)

                if self._target_hwnd and user32.IsWindow(self._target_hwnd):
                    if not self._is_hidden:
                        # Se o usuário clicou no botão de minimizar ('_')
                        if user32.IsIconic(self._target_hwnd):
                            # Oculta da barra de tarefas e da tela
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

            # Localiza a janela e define o ícone
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
        if sys.platform == "win32" and self._is_hidden and self._target_hwnd and user32.IsWindow(self._target_hwnd):
            try:
                user32.ShowWindow(self._target_hwnd, SW_RESTORE)
                user32.ShowWindow(self._target_hwnd, SW_SHOW)
            except Exception:
                pass

        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
