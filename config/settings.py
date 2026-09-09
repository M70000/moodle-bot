"""Módulo de configuração do Moodle AI Assistant.

Carrega e valida variáveis de ambiente com suporte a valores padrão para a UFMG.
"""

from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configurações da aplicação carregadas a partir de variáveis de ambiente ou .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Plataforma Moodle (UFMG Virtual)
    MOODLE_BASE_URL: str = Field(
        default="https://virtual.ufmg.br",
        description="URL base do Moodle/UFMG Virtual (ex: https://virtual.ufmg.br)"
    )

    # Discord Bot
    DISCORD_BOT_TOKEN: str = Field(
        default="",
        description="Token do Bot do Discord para notificações e revisões interativas"
    )
    DISCORD_CHANNEL_ID: int = Field(
        default=0,
        description="ID do canal privado do Discord para envio de alertas"
    )
    DISCORD_CONTENT_CHANNEL_ID: int = Field(
        default=0,
        description="ID do canal exclusivo para envio de materiais didáticos e resumos (/adicionarconteudo)"
    )

    # Google Gemini AI (com Fallback Hierárquico)
    GEMINI_API_KEY: str = Field(
        default="",
        description="Chave de API do Google Gemini (Google AI Studio)"
    )
    GEMINI_MODEL: str = Field(
        default="gemini-3.8-flash",
        description="Modelo principal (oficial) do Gemini para resolução"
    )
    GEMINI_FALLBACK_MODEL_1: str = Field(
        default="gemini-3.7-flash",
        description="Modelo secundário de fallback em caso de indisponibilidade"
    )
    GEMINI_FALLBACK_MODEL_2: str = Field(
        default="gemini-3.5-flash-lite",
        description="Modelo terciário de fallback (ultra rápido/leve)"
    )

    # Agendamento & Regras de Prazos
    CHECK_INTERVAL_MINUTES: int = Field(
        default=30,
        description="Intervalo em minutos para verificação periódica de novas atividades/materiais"
    )
    EMERGENCY_SUBMIT_ENABLED: bool = Field(
        default=False,
        description="Se verdadeiro, submete rascunho em T-1min se não houver resposta do usuário"
    )

    # Armazenamento Local
    STORAGE_COOKIES_PATH: Path = Field(
        default=Path("storage/cookies/session.json"),
        description="Caminho do arquivo com os cookies e sessão do Playwright"
    )
    STORAGE_MATERIALS_DIR: Path = Field(
        default=Path("storage/materials"),
        description="Diretório onde os materiais das disciplinas são salvos"
    )
    STORAGE_SUBMISSIONS_DIR: Path = Field(
        default=Path("storage/submissions"),
        description="Diretório onde os rascunhos de resolução gerados são salvos"
    )

    # Playwright / Automação de Login
    HEADLESS_LOGIN: bool = Field(
        default=False,
        description="Se verdadeiro, executa o login inicial em modo headless (não recomendado para SSO interativo)"
    )
    LOGIN_TIMEOUT_SECONDS: int = Field(
        default=300,
        description="Tempo máximo (em segundos) para o usuário concluir o login manual e 2FA no MinhaUFMG"
    )

    @field_validator("MOODLE_BASE_URL")
    @classmethod
    def normalize_moodle_url(cls, v: str) -> str:
        """Remove barra final se presente para padronização de URLs."""
        return v.rstrip("/")

    def ensure_storage_dirs(self) -> None:
        """Garante que todos os diretórios de armazenamento necessários existam."""
        self.STORAGE_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.STORAGE_MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
        self.STORAGE_SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)


# Instância global de configurações
settings = Settings()
settings.ensure_storage_dirs()
