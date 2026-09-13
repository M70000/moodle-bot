"""Testes unitários para o System Tray Icon e a Identidade Visual do Brasão de MG."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image

import pytest

from src.ui.tray_manager import SystemTrayManager
from src.scheduler.daemon import MoodleDaemon


def test_assets_existence_and_integrity():
    """Valida se todos os assets oficiais foram gerados e são íntegros."""
    root = Path(__file__).resolve().parent.parent
    logo_png = root / "assets" / "logo.png"
    tray_png = root / "assets" / "tray.png"
    app_ico = root / "assets" / "app.ico"
    static_logo = root / "src" / "ui" / "static" / "logo.png"
    static_favicon = root / "src" / "ui" / "static" / "favicon.ico"

    for path in [logo_png, tray_png, app_ico, static_logo, static_favicon]:
        assert path.exists(), f"Asset obrigatório ausente: {path}"
        assert path.stat().st_size > 0, f"Asset vazio: {path}"

    # Valida formato e transparência do logo
    with Image.open(logo_png) as img:
        assert img.format == "PNG"
        assert img.mode == "RGBA"
        assert img.size[0] == img.size[1], "Logo deve ter proporção quadrada 1:1"

    # Valida o ícone da bandeja
    with Image.open(tray_png) as img:
        assert img.size == (64, 64)

    # Valida arquivo ICO
    with Image.open(app_ico) as img:
        assert img.format == "ICO"


def test_tray_manager_initialization():
    """Testa a inicialização e estrutura do SystemTrayManager."""
    exit_called = False

    def on_exit():
        nonlocal exit_called
        exit_called = True

    tray = SystemTrayManager(on_exit_callback=on_exit)
    assert tray.logo_path.exists()
    assert tray.ico_path.exists()
    assert not tray._running

    # Simula start com mock do pystray para não bloquear em ambiente headless/CI
    with patch("pystray.Icon") as mock_icon_cls:
        mock_icon_instance = MagicMock()
        mock_icon_cls.return_value = mock_icon_instance

        with patch.object(tray, "_find_window_hwnd", return_value=12345):
            started = tray.start()
            assert started is True
            assert tray._running is True
            mock_icon_instance.run_detached.assert_called_once()

            # Testa restore e hide
            with patch("ctypes.windll.user32.ShowWindow") as mock_show:
                tray.restore_window()
                assert mock_show.called
                assert tray._is_hidden is False

                tray.hide_window()
                assert mock_show.called
                assert tray._is_hidden is True

            # Testa stop
            tray.stop()
            assert tray._running is False
            mock_icon_instance.stop.assert_called_once()


def test_daemon_tray_integration():
    """Testa se o MoodleDaemon respeita os parâmetros do SystemTray."""
    # Com tray habilitado
    daemon_with_tray = MoodleDaemon(enable_tray=True)
    assert daemon_with_tray.enable_tray is True
    assert daemon_with_tray.tray is None

    # Com tray desabilitado
    daemon_no_tray = MoodleDaemon(enable_tray=False)
    assert daemon_no_tray.enable_tray is False


def test_ui_static_html_and_css_branding():
    """Valida se o index.html e style.css incluem os favicons e estilos do Brasão."""
    root = Path(__file__).resolve().parent.parent
    html_file = root / "src" / "ui" / "static" / "index.html"
    css_file = root / "src" / "ui" / "static" / "style.css"

    html_content = html_file.read_text(encoding="utf-8")
    assert "logo.png" in html_content
    assert "favicon.ico" in html_content
    assert "brand-logo-container" in html_content
    assert "Brasão de Minas Gerais" in html_content

    css_content = css_file.read_text(encoding="utf-8")
    assert ".brand-logo-container" in css_content
    assert ".brand-logo-img" in css_content
