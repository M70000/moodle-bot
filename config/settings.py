"""Módulo de configuração do Moodle AI Assistant.

Carrega e valida variáveis de ambiente com suporte a valores padrão para a UFMG.
"""

from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Raíz absoluta do projeto (pai do diretório 'config/')
# Garante que os caminhos de armazenamento são sempre absolutos,
# independente do diretório de trabalho (CWD) de onde o bot é iniciado.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configurações da aplicação carregadas a partir de variáveis de ambiente ou .env."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
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
    DISCORD_ANNOUNCEMENTS_CHANNEL_ID: int = Field(
        default=0,
        description="ID do canal exclusivo para avisos e comunicados dos professores (se 0, usa DISCORD_CHANNEL_ID)"
    )
    DISCORD_QUEUE_CHANNEL_ID: int = Field(
        default=0,
        description="ID do canal exclusivo para acompanhamento da fila de tarefas em tempo real (se 0, usa DISCORD_CHANNEL_ID)"
    )
    DISCORD_STUDY_CHANNEL_ID: int = Field(
        default=0,
        description="ID do canal exclusivo para estudos, dúvidas (/perguntar), simulados (/quiz) e flashcards (/flashcards) (se 0, responde no canal chamado)"
    )

    # Ponte Nuvem (Render Hub <-> Desktop Runner)
    RENDER_URL: str = Field(
        default="",
        description="URL pública do serviço Render (ex: https://seu-bot.onrender.com) para sincronização de submissões remotas"
    )

    # Integração Notion (Central de Estudos & Tarefas)
    NOTION_API_KEY: str = Field(
        default="",
        description="Token de integração do Notion (Secret API Key)"
    )
    NOTION_PAGE_ID: str = Field(
        default="",
        description="ID da página principal no Notion (Minha Central)"
    )
    NOTION_TASKS_DATABASE_ID: str = Field(
        default="",
        description="ID da database de Tarefas / À Fazer no Notion"
    )
    NOTION_COURSES_DATABASE_ID: str = Field(
        default="",
        description="ID da database de Cursos / Disciplinas no Notion"
    )
    NOTION_DAILY_CHECKLIST_BLOCK_ID: str = Field(
        default="",
        description="ID do bloco toggle 'tarefas do dia' na página central do Notion"
    )
    NOTION_WEEKLY_SCHEDULE_TABLE_ID: str = Field(
        default="",
        description="ID da tabela 'Agenda Semanal' na página central do Notion"
    )

    # Provedor de IA Principal e Cadeia de Fallback (BYOK Multi-Provider)
    AI_PROVIDER: str = Field(
        default="gemini",
        description="Provedor de IA principal: 'gemini', 'claude' (ou 'anthropic'), 'deepseek'"
    )
    AI_FALLBACK_PROVIDER_1: str = Field(
        default="gemini",
        description="Primeiro provedor de contingência: 'gemini', 'claude', 'deepseek', 'none'"
    )
    AI_FALLBACK_PROVIDER_2: str = Field(
        default="deepseek",
        description="Segundo provedor de contingência: 'gemini', 'claude', 'deepseek', 'none'"
    )
    AI_FALLBACK_PROVIDER_3: str = Field(
        default="none",
        description="Terceiro provedor de contingência: 'gemini', 'claude', 'deepseek', 'none'"
    )

    # Google Gemini AI (com Fallback Hierárquico)
    GEMINI_API_KEY: str = Field(
        default="",
        description="Chave de API do Google Gemini (Google AI Studio)"
    )
    GEMINI_MODEL: str = Field(
        default="gemini-3.5-flash",
        description="Modelo principal (oficial) do Gemini para resolução"
    )
    GEMINI_FALLBACK_MODEL_1: str = Field(
        default="gemini-3.8-flash",
        description="Modelo secundário de fallback em caso de indisponibilidade"
    )
    GEMINI_FALLBACK_MODEL_2: str = Field(
        default="gemini-3.7-flash",
        description="Modelo terciário de fallback (raciocínio)"
    )
    GEMINI_FALLBACK_MODEL_3: str = Field(
        default="gemini-3.5-flash-lite",
        description="Modelo quaternário de fallback (rede de segurança)"
    )
    GEMINI_TIMEOUT_SECONDS: int = Field(
        default=90,
        description="Tempo limite em segundos para o modelo principal responder"
    )
    GEMINI_FALLBACK_TIMEOUT_SECONDS: int = Field(
        default=60,
        description="Tempo limite em segundos para cada modelo de fallback responder"
    )
    GEMINI_FALLBACK_DELAY_SECONDS: float = Field(
        default=2.0,
        description="Intervalo em segundos entre tentativas de modelos de fallback"
    )

    # Anthropic Claude (BYOK alternativo)
    ANTHROPIC_API_KEY: str = Field(
        default="",
        description="Chave de API do Anthropic Claude (opcional — substitui Gemini como agente IA)"
    )
    ANTHROPIC_MODEL: str = Field(
        default="claude-haiku-4-5",
        description="Modelo Claude a usar (claude-opus-5, claude-sonnet-5, claude-haiku-4-5)"
    )

    # DeepSeek (BYOK alternativo)
    DEEPSEEK_API_KEY: str = Field(
        default="",
        description="Chave de API do DeepSeek (opcional — substitui Gemini como agente IA)"
    )
    DEEPSEEK_MODEL: str = Field(
        default="deepseek-chat",
        description="Modelo DeepSeek a usar (deepseek-chat, deepseek-reasoner)"
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
        default=PROJECT_ROOT / "storage" / "cookies" / "session.json",
        description="Caminho do arquivo com os cookies e sessão do Playwright"
    )
    STORAGE_MATERIALS_DIR: Path = Field(
        default=PROJECT_ROOT / "storage" / "materials",
        description="Diretório onde os materiais das disciplinas são salvos"
    )
    STORAGE_SUBMISSIONS_DIR: Path = Field(
        default=PROJECT_ROOT / "storage" / "submissions",
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

    @field_validator("STORAGE_COOKIES_PATH", "STORAGE_MATERIALS_DIR", "STORAGE_SUBMISSIONS_DIR", mode="before")
    @classmethod
    def make_absolute_path(cls, v) -> Path:
        """Converte caminhos relativos do .env para absolutos baseados na raiz do projeto.

        Garante que storage/materials no .env resolva para C:\\moodle-bot\\storage\\materials
        independente do diretório de trabalho (CWD) de onde o bot é iniciado.
        """
        p = Path(v)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def ensure_storage_dirs(self) -> None:
        """Garante que todos os diretórios de armazenamento necessários existam."""
        self.STORAGE_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.STORAGE_MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
        self.STORAGE_SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)



# Instância global de configurações
settings = Settings()
settings.ensure_storage_dirs()
