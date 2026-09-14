"""Módulo de Identidade Visual e Tema do LumiBot.

Centraliza a paleta de cores oficial, emojis e utilitários de formatação de Embeds
para manter consistência e modernidade em toda a interface do Discord.
"""

from datetime import datetime, timezone
from typing import Optional
import discord


class LumiTheme:
    """Paleta de cores e emojis oficiais do LumiBot."""

    # Paleta de Cores Institucional
    PRIMARY = discord.Color(0xF59E0B)    # Lumi Gold / Amber (Destaque acadêmico, clareza)
    SECONDARY = discord.Color(0x6366F1)  # Indigo / Roxo moderno (Neutro, informativo)
    SUCCESS = discord.Color(0x10B981)    # Verde esmeralda (Concluído, ativo, êxito)
    WARNING = discord.Color(0xEF4444)    # Vermelho vibrante (Prazos atrasados/próximos, erros)

    # Emojis Padronizados
    EMOJI_LUMI = "💡"
    EMOJI_MAGIC = "✨"
    EMOJI_DEADLINE = "📅"
    EMOJI_COURSES = "📚"
    EMOJI_REMINDER = "🔔"
    EMOJI_STATUS = "🛰️"
    EMOJI_CHANNELS = "🔒"

    # Rodapé Institucional
    FOOTER_TEXT = "LumiBot • Seu Copiloto Acadêmico"


def apply_lumi_footer(embed: discord.Embed, extra_info: Optional[str] = None) -> discord.Embed:
    """Aplica o rodapé institucional do LumiBot e garante o timestamp no embed."""
    if extra_info:
        footer_content = f"{LumiTheme.FOOTER_TEXT} • {extra_info}"
    else:
        footer_content = LumiTheme.FOOTER_TEXT

    embed.set_footer(text=footer_content)
    if not embed.timestamp:
        embed.timestamp = datetime.now(timezone.utc)
    return embed


def create_lumi_embed(
    title: str,
    description: Optional[str] = None,
    color: Optional[discord.Color] = None,
    extra_footer: Optional[str] = None,
    **kwargs
) -> discord.Embed:
    """Cria um discord.Embed padronizado com as cores e rodapé institucional do LumiBot."""
    c = color or LumiTheme.PRIMARY
    embed = discord.Embed(title=title, description=description, color=c, **kwargs)
    apply_lumi_footer(embed, extra_info=extra_footer)
    return embed
