"""Testes unitários para provisionamento de salas privadas no Discord via !meuscanais."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.notifier.discord_bot import (
    get_user_provisioned_channels,
    provision_user_channels,
)
# Nota: detect_discord_user_channels foi removida da UI (auto-detect desabilitado).
# Canais são criados exclusivamente pelo comando !meuscanais no Discord.



class TestChannelProvisioning(unittest.TestCase):

    def test_provision_user_channels_new_category(self):
        # Simula Guild e Member do Discord
        mock_guild = MagicMock(spec=discord.Guild)
        mock_guild.id = 123456
        mock_guild.name = "Servidor Moodle Turma"
        mock_guild.categories = []

        mock_member = MagicMock(spec=discord.Member)
        mock_member.id = 789012
        mock_member.name = "joaosilva"
        mock_member.display_name = "João Silva"
        mock_member.mention = "<@789012>"
        mock_member.bot = False

        mock_category = MagicMock(spec=discord.CategoryChannel)
        mock_category.id = 999001
        mock_category.name = "🔒 Moodle • João Silva"
        mock_category.text_channels = []

        mock_guild.create_category = AsyncMock(return_value=mock_category)

        # Simula criação dos 5 canais
        created_channels = []
        for idx, name in enumerate(["alertas-revisoes", "conteudos", "avisos-turma", "fila-tarefas", "estudos-simulados"]):
            ch = MagicMock(spec=discord.TextChannel)
            ch.id = 888000 + idx
            ch.name = name
            ch.send = AsyncMock()
            created_channels.append(ch)

        mock_guild.create_text_channel = AsyncMock(side_effect=created_channels)
        # Mock fetch_member (chamado agora via REST para garantir objeto Member completo)
        mock_guild.fetch_member = AsyncMock(return_value=mock_member)
        mock_guild.me = MagicMock()

        result = asyncio.run(provision_user_channels(mock_guild, mock_member))

        # 1. Verifica se a categoria foi criada com o nome esperado
        mock_guild.create_category.assert_called_once()
        self.assertIn("João Silva", mock_guild.create_category.call_args.kwargs["name"])

        # 2. Verifica se os 5 canais foram criados dentro da categoria
        self.assertEqual(mock_guild.create_text_channel.call_count, 5)

        # 3. Verifica o mapeamento retornado
        ch_map = result["channels"]
        self.assertIn("DISCORD_CHANNEL_ID", ch_map)
        self.assertIn("DISCORD_CONTENT_CHANNEL_ID", ch_map)
        self.assertIn("DISCORD_ANNOUNCEMENTS_CHANNEL_ID", ch_map)
        self.assertIn("DISCORD_QUEUE_CHANNEL_ID", ch_map)
        self.assertIn("DISCORD_STUDY_CHANNEL_ID", ch_map)

        self.assertEqual(ch_map["DISCORD_CHANNEL_ID"], 888000)
        self.assertEqual(ch_map["DISCORD_STUDY_CHANNEL_ID"], 888004)

    def test_get_user_provisioned_channels_lookup(self):
        # Simula bot em memória com categoria existente
        mock_bot = MagicMock()
        mock_bot.is_ready.return_value = True

        mock_guild = MagicMock()
        mock_guild.id = 111222
        mock_guild.name = "Turma Engenharia"

        mock_member = MagicMock()
        mock_member.id = 555666
        mock_member.name = "anaclara"
        mock_member.display_name = "Ana Clara"

        mock_category = MagicMock()
        mock_category.name = "🔒 Moodle • Ana Clara"

        mock_channels = []
        for name, cid in [
            ("alertas-revisoes", 101),
            ("conteudos", 102),
            ("avisos-turma", 103),
            ("fila-tarefas", 104),
            ("estudos-simulados", 105),
        ]:
            c = MagicMock()
            c.name = name
            c.id = cid
            mock_channels.append(c)

        mock_category.text_channels = mock_channels
        mock_guild.categories = [mock_category]
        mock_guild.members = [mock_member]
        mock_bot.guilds = [mock_guild]

        with patch("src.notifier.discord_bot.bot", mock_bot):
            res = get_user_provisioned_channels("anaclara")
            self.assertIsNotNone(res)
            self.assertEqual(res["category_name"], "🔒 Moodle • Ana Clara")
            self.assertEqual(res["channels"]["DISCORD_CHANNEL_ID"], "101")
            self.assertEqual(res["channels"]["DISCORD_STUDY_CHANNEL_ID"], "105")


if __name__ == "__main__":
    unittest.main()
