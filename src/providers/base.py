"""Contrato abstrato de provedor de LMS (BaseLMSProvider) para o LumiBot.

Define a interface universal assíncrona que todos os adaptadores de plataformas
educacionais (Moodle, Canvas LMS, etc.) devem obrigatoriamente implementar.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.core.models import LMSCourse, LMSAssignment, LMSAnnouncement


class BaseLMSProvider(ABC):
    """Classe base abstrata para adaptadores de plataformas educacionais (LMS)."""

    @property
    @abstractmethod
    def platform(self) -> str:
        """Identificador textual da plataforma (ex: 'moodle', 'canvas')."""
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """Testa se a autenticação e conectividade com o LMS estão ativas e válidas.

        Returns:
            bool: True se conectado e autenticado com sucesso, False caso contrário.
        """
        pass

    @abstractmethod
    async def get_courses(self) -> List[LMSCourse]:
        """Obtém a lista de cursos/disciplinas ativas do usuário matriculado.

        Returns:
            List[LMSCourse]: Lista de disciplinas padronizadas.
        """
        pass

    @abstractmethod
    async def get_upcoming_assignments(
        self,
        course_id: Optional[str] = None,
        days: int = 7
    ) -> List[LMSAssignment]:
        """Obtém tarefas e prazos futuros dentro de uma janela temporal.

        Args:
            course_id: Opcional, filtra tarefas de uma disciplina específica.
            days: Janela em dias a partir de agora para considerar prazos futuros.

        Returns:
            List[LMSAssignment]: Lista de atividades ordenadas por prazo.
        """
        pass

    @abstractmethod
    async def get_announcements(
        self,
        course_id: Optional[str] = None,
        limit: int = 5
    ) -> List[LMSAnnouncement]:
        """Obtém avisos e comunicados recentes dos professores.

        Args:
            course_id: Opcional, filtra comunicados de uma disciplina específica.
            limit: Número máximo de comunicados a retornar por disciplina ou globalmente.

        Returns:
            List[LMSAnnouncement]: Lista de comunicados padronizados.
        """
        pass

    async def close(self) -> None:
        """Encerra recursos de conexão, sessões HTTP ou navegadores abertos.

        Implementação padrão é no-op, devendo ser sobrescrita caso o adaptador
        mantenha conexões persistentes.
        """
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
