"""Testes unitários para o mecanismo de Keep-Alive / Heartbeat de Sessão do Moodle."""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from src.auth.moodle_auth import MoodleAuth
from src.notifier.discord_bot import MoodleDiscordNotifier
from src.scheduler.daemon import MoodleDaemon


class TestSessionHeartbeat(unittest.IsolatedAsyncioTestCase):
    """Testes da rotina de heartbeat e keep-alive em segundo plano."""

    async def test_heartbeat_no_session_file(self):
        """Se o arquivo de sessão não existir, o heartbeat deve falhar imediatamente."""
        fake_path = Path("storage/cookies/nonexistent_heartbeat_session_test.json")
        if fake_path.exists():
            fake_path.unlink()

        auth = MoodleAuth(cookies_path=fake_path)
        active, msg = await auth.heartbeat_session()
        self.assertFalse(active)
        self.assertIn("não encontrado", msg.lower())

    async def test_heartbeat_success_renews_storage(self):
        """Com sessão válida, deve manter ativa e persistir novo storage_state."""
        session_file = Path("storage/cookies/test_heartbeat_success.json")
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

        try:
            auth = MoodleAuth(cookies_path=session_file)

            mock_response = MagicMock()
            mock_response.ok = True
            mock_response.status = 200
            mock_response.url = "https://virtual.ufmg.br/minhasturmas"

            mock_req_ctx = AsyncMock()
            mock_req_ctx.get = AsyncMock(return_value=mock_response)
            mock_req_ctx.storage_state = AsyncMock()

            mock_playwright = AsyncMock()
            mock_playwright.request.new_context = AsyncMock(return_value=mock_req_ctx)

            mock_pw_cm = AsyncMock()
            mock_pw_cm.__aenter__.return_value = mock_playwright
            mock_pw_cm.__aexit__.return_value = None

            with patch("src.auth.moodle_auth.async_playwright", return_value=mock_pw_cm):
                active, msg = await auth.heartbeat_session(save_refreshed=True)

            self.assertTrue(active)
            self.assertIn("ativa e renovada", msg.lower())
            mock_req_ctx.storage_state.assert_called_once_with(path=str(session_file))
        finally:
            if session_file.exists():
                session_file.unlink()

    async def test_heartbeat_detects_expired_idp_redirect(self):
        """Se redirecionar para tela de login do IDP, deve detectar expiração."""
        session_file = Path("storage/cookies/test_heartbeat_expired.json")
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text('{"cookies": []}', encoding="utf-8")

        try:
            auth = MoodleAuth(cookies_path=session_file)

            mock_response = MagicMock()
            mock_response.ok = True
            mock_response.status = 200
            mock_response.url = "https://sistemas.ufmg.br/idp/login.jsp"

            mock_req_ctx = AsyncMock()
            mock_req_ctx.get = AsyncMock(return_value=mock_response)

            mock_playwright = AsyncMock()
            mock_playwright.request.new_context = AsyncMock(return_value=mock_req_ctx)

            mock_pw_cm = AsyncMock()
            mock_pw_cm.__aenter__.return_value = mock_playwright
            mock_pw_cm.__aexit__.return_value = None

            with patch("src.auth.moodle_auth.async_playwright", return_value=mock_pw_cm):
                active, msg = await auth.heartbeat_session()

            self.assertFalse(active)
            self.assertIn("expirada", msg.lower())
        finally:
            if session_file.exists():
                session_file.unlink()

    async def test_heartbeat_fallback_to_validate_on_network_error(self):
        """Se houver erro de rede no request_context, faz fallback para validate_session."""
        session_file = Path("storage/cookies/test_heartbeat_fallback.json")
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text('{"cookies": []}', encoding="utf-8")

        try:
            auth = MoodleAuth(cookies_path=session_file)

            mock_playwright = AsyncMock()
            mock_playwright.request.new_context.side_effect = Exception("Conexão falhou")

            mock_pw_cm = AsyncMock()
            mock_pw_cm.__aenter__.return_value = mock_playwright
            mock_pw_cm.__aexit__.return_value = None

            with patch("src.auth.moodle_auth.async_playwright", return_value=mock_pw_cm), \
                 patch.object(auth, "validate_session", AsyncMock(return_value=(True, "Usuario Teste"))):
                active, msg = await auth.heartbeat_session()

            self.assertTrue(active)
            self.assertIn("Usuario Teste", msg)
        finally:
            if session_file.exists():
                session_file.unlink()

    async def test_daemon_heartbeat_alert_debounced(self):
        """O daemon deve disparar alerta de expiração no Discord com debounce (apenas uma vez)."""
        daemon = MoodleDaemon()
        daemon.auth.heartbeat_session = AsyncMock(return_value=(False, "Sessão expirada"))
        daemon.notifier.send_session_expired_alert = AsyncMock(return_value=True)
        daemon.notifier.token = "fake_token"
        daemon.notifier.channel_id = 123456

        # Primeiro ciclo do heartbeat com falha -> deve disparar alerta
        await daemon.session_heartbeat_job()
        self.assertTrue(daemon._session_expired_alerted)
        self.assertEqual(daemon.notifier.send_session_expired_alert.call_count, 1)

        # Segundo ciclo consecutivo com falha -> NÃO deve disparar novamente (debounce)
        await daemon.session_heartbeat_job()
        self.assertTrue(daemon._session_expired_alerted)
        self.assertEqual(daemon.notifier.send_session_expired_alert.call_count, 1)

        # Sessão restabelecida -> reseta flag
        daemon.auth.heartbeat_session = AsyncMock(return_value=(True, "Sessão renovada"))
        await daemon.session_heartbeat_job()
        self.assertFalse(daemon._session_expired_alerted)

    async def test_send_session_expired_alert_discord(self):
        """Testa montagem e envio da notificação de sessão expirada no Discord."""
        notifier = MoodleDiscordNotifier(token="mock_token", channel_id=987654321)
        mock_channel = AsyncMock()
        mock_channel.send = AsyncMock()

        with patch.object(notifier, "_resolve_channel", AsyncMock(return_value=mock_channel)):
            success = await notifier.send_session_expired_alert()

        self.assertTrue(success)
        self.assertEqual(mock_channel.send.call_count, 1)
        call_kwargs = mock_channel.send.call_args[1]
        self.assertIn("Atenção", call_kwargs.get("content", ""))
        embed = call_kwargs.get("embed")
        self.assertIsNotNone(embed)
        self.assertIn("Expirada", embed.title)
