"""Adaptador Moodle (MoodleAdapter) para o LumiBot.

Implementa BaseLMSProvider encapsulando os módulos de autenticação (MoodleAuth)
e extração (MoodleScraper), mantendo 100% da compatibilidade e estabilidade
da automação existente do Moodle / UFMG Virtual.
"""

from datetime import datetime, timedelta
from typing import List, Optional
from playwright.async_api import async_playwright

from src.core.models import (
    LMSCourse,
    LMSAssignment,
    LMSAnnouncement,
    from_moodle_course,
    from_moodle_assignment,
    from_moodle_announcement,
)
from src.providers.base import BaseLMSProvider
from src.auth.moodle_auth import MoodleAuth
from src.scraper.moodle_scraper import MoodleScraper, Course


class MoodleAdapter(BaseLMSProvider):
    """Adaptador de integração para o Moodle UFMG Virtual."""

    def __init__(
        self,
        auth: Optional[MoodleAuth] = None,
        scraper: Optional[MoodleScraper] = None
    ):
        self.auth = auth or MoodleAuth()
        self.scraper = scraper or MoodleScraper(auth=self.auth)

    @property
    def platform(self) -> str:
        return "moodle"

    async def test_connection(self) -> bool:
        """Verifica se a sessão do Moodle existe e ainda é válida."""
        if not self.auth.session_exists:
            return False
        try:
            is_valid, _ = await self.auth.validate_session()
            return bool(is_valid)
        except Exception:
            return False

    async def get_courses(self) -> List[LMSCourse]:
        """Obtém as disciplinas do semestre atual no Moodle."""
        courses = await self.scraper.list_courses()
        return [from_moodle_course(c) for c in courses]

    async def get_upcoming_assignments(
        self,
        course_id: Optional[str] = None,
        days: int = 7
    ) -> List[LMSAssignment]:
        """Obtém as tarefas acadêmicas do Moodle dentro da janela de dias especificada."""
        if not self.auth.session_exists:
            return []

        all_courses: List[Course] = await self.scraper.list_courses()
        if course_id:
            target_courses = [c for c in all_courses if str(c.id) == str(course_id)]
        else:
            target_courses = all_courses

        if not target_courses:
            return []

        now = datetime.now()
        cutoff = now + timedelta(days=days) if days > 0 else None
        upcoming: List[LMSAssignment] = []

        async with async_playwright() as p:
            browser, context = await self.auth.get_authenticated_context(p, headless=True)
            try:
                page = await context.new_page()
                for course in target_courses:
                    try:
                        raw_assignments = await self.scraper.get_course_assignments(course, context, page)
                        for a in raw_assignments:
                            lms_a = from_moodle_assignment(a)
                            # Se days foi definido, filtra tarefas dentro do prazo
                            if cutoff and lms_a.due_date:
                                # Normaliza para naive datetime se necessário para comparação
                                cmp_due = lms_a.due_date.replace(tzinfo=None) if lms_a.due_date.tzinfo else lms_a.due_date
                                if cmp_due > cutoff:
                                    continue
                                if cmp_due < now and not lms_a.is_submitted:
                                    # Tarefa já expirada
                                    continue
                            upcoming.append(lms_a)
                    except Exception:
                        continue
            finally:
                await browser.close()

        # Ordena por prazo de entrega (as mais próximas primeiro)
        upcoming.sort(key=lambda x: (x.due_date is None, x.due_date or datetime.max))
        return upcoming

    async def get_announcements(
        self,
        course_id: Optional[str] = None,
        limit: int = 5
    ) -> List[LMSAnnouncement]:
        """Obtém os comunicados dos fóruns das disciplinas do Moodle."""
        if not self.auth.session_exists:
            return []

        all_courses: List[Course] = await self.scraper.list_courses()
        if course_id:
            target_courses = [c for c in all_courses if str(c.id) == str(course_id)]
        else:
            target_courses = all_courses

        if not target_courses:
            return []

        announcements: List[LMSAnnouncement] = []

        async with async_playwright() as p:
            browser, context = await self.auth.get_authenticated_context(p, headless=True)
            try:
                page = await context.new_page()
                for course in target_courses:
                    try:
                        raw_anns = await self.scraper.get_course_announcements(
                            course, context, page
                        )
                        for an in raw_anns[:limit]:
                            announcements.append(from_moodle_announcement(an))
                    except Exception:
                        continue
            finally:
                await browser.close()

        return announcements[:limit]
