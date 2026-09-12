"""Testes unitários para persistência de materiais (/adicionarconteudo), Cloud Bridge e restauração."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.notifier.bridge_manager import CloudBridgeManager
from src.notifier.discord_bot import resolve_course_materials_dir, restore_materials_from_discord
from src.scheduler.bridge_runner import BridgeRunner
from src.scheduler.state import DaemonState


class TestAdicionarConteudoPersistence(unittest.IsolatedAsyncioTestCase):
    """Testa o ciclo completo de persistência de materiais adicionados pelo Discord."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self.state_file = self.root_path / "state.json"
        self.materials_dir = self.root_path / "materials"
        self.materials_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_state_register_and_get_custom_materials(self):
        """Valida gravação, atualização e recarregamento de materiais no DaemonState."""
        state = DaemonState(file_path=self.state_file)

        item = state.register_custom_material(
            course="2026_2 - CÁLCULO 2 - METATURMA",
            filename="resumo_p1.pdf",
            attachment_url="https://cdn.discordapp.com/attachments/111/222/resumo_p1.pdf",
            channel_id=111,
            message_id=222,
            uploader="Aluno#1234",
            size=102400
        )

        self.assertEqual(item["filename"], "resumo_p1.pdf")
        self.assertEqual(item["size"], 102400)

        # Recupera por disciplina
        course_mats = state.get_custom_materials(course="2026_2 - CÁLCULO 2 - METATURMA")
        self.assertEqual(len(course_mats), 1)
        self.assertEqual(course_mats[0]["filename"], "resumo_p1.pdf")

        # Recupera todos
        all_mats = state.get_custom_materials()
        self.assertEqual(len(all_mats), 1)
        self.assertEqual(all_mats[0]["course"], "2026_2 - CÁLCULO 2 - METATURMA")

        # Atualização do mesmo arquivo
        state.register_custom_material(
            course="2026_2 - CÁLCULO 2 - METATURMA",
            filename="resumo_p1.pdf",
            attachment_url="https://cdn.discordapp.com/attachments/111/333/resumo_p1.pdf",
            size=104000
        )
        reloaded = DaemonState(file_path=self.state_file)
        mats_reloaded = reloaded.get_custom_materials("2026_2 - CÁLCULO 2 - METATURMA")
        self.assertEqual(len(mats_reloaded), 1)
        self.assertEqual(mats_reloaded[0]["size"], 104000)
        self.assertEqual(mats_reloaded[0]["attachment_url"], "https://cdn.discordapp.com/attachments/111/333/resumo_p1.pdf")

    def test_resolve_course_materials_dir_fuzzy(self):
        """Valida que nomes parciais ou aproximados encontram a pasta canônica correta."""
        canonical_dir = self.materials_dir / "2026_2 - CÁLCULO 2 - METATURMA"
        canonical_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.notifier.discord_bot.settings.STORAGE_MATERIALS_DIR", self.materials_dir):
            # 1. Busca por nome curto aproximado
            res = resolve_course_materials_dir("Cálculo 2")
            self.assertEqual(res, canonical_dir)

            # 2. Busca por nome exato
            res_exact = resolve_course_materials_dir("2026_2 - CÁLCULO 2 - METATURMA")
            self.assertEqual(res_exact, canonical_dir)

            # 3. Busca por disciplina inexistente cria nova pasta sanitizada
            res_new = resolve_course_materials_dir("Estatística Avançada")
            self.assertTrue(res_new.exists())
            self.assertEqual(res_new.name, "Estatistica_Avancada")

    async def test_cloud_bridge_materials_lifecycle(self):
        """Testa o armazenamento e consulta de materiais customizados na Cloud Bridge."""
        manager = CloudBridgeManager()

        mat_payload = {
            "course": "Física Teórica",
            "filename": "formulario.pdf",
            "attachment_url": "https://cdn.discordapp.com/att/formulario.pdf",
            "channel_id": 555,
            "message_id": 777,
            "uploader": "Estudante",
            "size": 50000
        }

        await manager.register_material(mat_payload)
        mats = await manager.get_custom_materials()
        self.assertEqual(len(mats), 1)
        self.assertEqual(mats[0]["filename"], "formulario.pdf")

        # Publica estado completo incluindo materiais
        await manager.publish_state(
            courses=["Física Teórica"],
            assignments={},
            custom_materials=[
                mat_payload,
                {"course": "Química", "filename": "tabela.pdf", "attachment_url": "https://cdn/tabela.pdf"}
            ]
        )

        all_mats = await manager.get_custom_materials()
        self.assertEqual(len(all_mats), 2)
        status = await manager.desktop_status()
        self.assertEqual(status["custom_materials_count"], 2)

    @patch("src.scheduler.bridge_runner.urllib.request.urlopen")
    async def test_bridge_runner_sync_custom_materials_from_hub(self, mock_urlopen):
        """Valida que o BridgeRunner baixa materiais da nuvem que não existem localmente."""
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [
            # 1. Resposta da rota /api/bridge/materials
            json.dumps({
                "materials": [{
                    "course": "2026_2 - CÁLCULO 2 - METATURMA",
                    "filename": "gabarito_lista1.pdf",
                    "attachment_url": "https://cdn.discordapp.com/files/gabarito_lista1.pdf",
                    "channel_id": "123",
                    "message_id": "456",
                    "uploader": "Professor",
                    "size": 2048
                }]
            }).encode("utf-8"),
            # 2. Conteúdo binário do arquivo baixado
            b"%PDF-1.4 Mock PDF Content"
        ]
        mock_urlopen.return_value = mock_resp

        canonical_dir = self.materials_dir / "2026_2 - CÁLCULO 2 - METATURMA"
        canonical_dir.mkdir(parents=True, exist_ok=True)

        runner = BridgeRunner(render_url="https://mock-render.onrender.com")

        with patch("src.notifier.discord_bot.settings.STORAGE_MATERIALS_DIR", self.materials_dir), \
             patch("src.scheduler.state.STATE_FILE", self.state_file):
            downloaded = await runner.sync_custom_materials_from_hub()
            self.assertEqual(downloaded, 1)

            downloaded_file = canonical_dir / "gabarito_lista1.pdf"
            self.assertTrue(downloaded_file.exists())
            self.assertEqual(downloaded_file.read_bytes(), b"%PDF-1.4 Mock PDF Content")

    async def test_restore_materials_from_discord(self):
        """Valida restauração automática a partir do histórico de mensagens do canal do Discord."""
        canonical_dir = self.materials_dir / "2026_2 - CÁLCULO 2 - METATURMA"
        canonical_dir.mkdir(parents=True, exist_ok=True)

        # Mock de bot, guild, canal e mensagem com anexo
        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_guild = MagicMock()
        mock_channel = MagicMock()
        mock_channel.name = "conteudos"
        mock_channel.id = 999888
        mock_guild.text_channels = [mock_channel]
        mock_client.guilds = [mock_guild]
        mock_client.get_channel.return_value = mock_channel

        # Simula anexo do Discord
        mock_attachment = AsyncMock()
        mock_attachment.filename = "resumo_aula1.pdf"
        mock_attachment.url = "https://cdn.discordapp.com/att/resumo_aula1.pdf"
        mock_attachment.size = 5120

        async def fake_save(fp):
            Path(fp).write_bytes(b"%PDF Resumo restaurado")
        mock_attachment.save.side_effect = fake_save

        # Simula mensagem de bot com confirmação de /adicionarconteudo
        mock_msg = MagicMock()
        mock_msg.id = 123456
        mock_msg.author = mock_client.user
        mock_msg.attachments = [mock_attachment]
        mock_field = MagicMock()
        mock_field.name = "🏫 Disciplina"
        mock_field.value = "2026_2 - CÁLCULO 2 - METATURMA"
        mock_embed = MagicMock()
        mock_embed.title = "📥 Conteúdo Adicionado à Base de Conhecimento!"
        mock_embed.fields = [mock_field]
        mock_msg.embeds = [mock_embed]

        async def fake_history(limit=100):
            yield mock_msg
        mock_channel.history = fake_history

        with patch("src.notifier.discord_bot.settings.STORAGE_MATERIALS_DIR", self.materials_dir), \
             patch("src.notifier.discord_bot.settings.DISCORD_CONTENT_CHANNEL_ID", 999888), \
             patch("src.scheduler.state.STATE_FILE", self.state_file):
            restored = await restore_materials_from_discord(mock_client)
            self.assertEqual(restored, 1)

            target_file = canonical_dir / "resumo_aula1.pdf"
            self.assertTrue(target_file.exists())
            self.assertEqual(target_file.read_bytes(), b"%PDF Resumo restaurado")


if __name__ == "__main__":
    unittest.main()
