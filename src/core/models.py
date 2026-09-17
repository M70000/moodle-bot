"""Modelos de dados neutros para arquitetura Multi-LMS do LumiBot.

Padroniza as estruturas de dados para isolar diferenças entre provedores
educacionais (Moodle, Canvas LMS, etc.).
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
import re
from pydantic import BaseModel, Field


def sanitize_filename(name: str) -> str:
    """Remove caracteres inválidos para criação segura de arquivos e diretórios."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(". ")
    return (cleaned[:100] if len(cleaned) > 100 else cleaned) or "arquivo"


class LMSCourse(BaseModel):
    """Representa uma disciplina ou curso padronizado em qualquer LMS."""

    id: str = Field(..., description="Identificador único da disciplina no LMS")
    name: str = Field(..., description="Nome de exibição completo da disciplina")
    code: str = Field(default="", description="Código da disciplina (ex: INF1005, DCC207)")
    platform: Literal["moodle", "canvas"] | str = Field(
        default="moodle",
        description="Plataforma de origem da disciplina ('moodle' ou 'canvas')"
    )
    url: str = Field(default="", description="URL de acesso à página principal da disciplina")
    term: Optional[str] = Field(default=None, description="Período/semestre letivo (ex: 2026.1, 2026_2)")

    @property
    def safe_name(self) -> str:
        """Nome sanitizado para uso seguro em caminhos de pastas locais."""
        return sanitize_filename(self.name)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa o curso em formato compatível com o ecossistema LumiBot."""
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "platform": self.platform,
            "url": self.url,
            "term": self.term,
        }


class LMSAssignment(BaseModel):
    """Representa uma atividade, tarefa avaliativa ou questionário com prazo de entrega."""

    id: str = Field(..., description="Identificador único da atividade no LMS")
    course_id: str = Field(default="", description="ID da disciplina a que a atividade pertence")
    course_name: str = Field(..., description="Nome da disciplina associada")
    title: str = Field(..., description="Título da atividade ou questionário")
    description: str = Field(default="", description="Enunciado ou descrição detalhada")
    due_date: Optional[datetime] = Field(
        default=None,
        description="Data e horário limite de entrega com fuso horário (timezone-aware)"
    )
    is_submitted: bool = Field(default=False, description="Indica se o aluno já enviou a resolução")
    url: str = Field(default="", description="URL direta da atividade no LMS")
    platform: Literal["moodle", "canvas"] | str = Field(
        default="moodle",
        description="Plataforma de origem da atividade"
    )
    activity_type: str = Field(
        default="assign",
        description="Tipo de atividade ('assign', 'quiz', 'discussion', etc.)"
    )
    points_possible: Optional[float] = Field(
        default=None,
        description="Pontuação máxima atribuível à atividade"
    )
    submission_status: str = Field(
        default="Não enviado",
        description="Status textual da submissão (ex: 'Enviado', 'Pendente')"
    )
    grading_status: Optional[str] = Field(
        default=None,
        description="Status de avaliação do professor"
    )
    grade_value: Optional[str] = Field(
        default=None,
        description="Nota recebida caso já avaliado"
    )
    time_remaining: Optional[str] = Field(
        default=None,
        description="Texto descritivo do tempo restante (ex: '3 dias restantes')"
    )
    due_date_str: Optional[str] = Field(
        default=None,
        description="Representação em string da data de entrega"
    )
    submission_types: List[str] = Field(
        default_factory=list,
        description="Tipos de submissão permitidos ('online_url', 'online_upload', 'online_text_entry', etc.)"
    )

    @property
    def course(self) -> str:
        """Alias de compatibilidade com o formato legado Moodle."""
        return self.course_name

    @property
    def is_expired(self) -> bool:
        """Indica se a data de entrega da atividade já expirou."""
        if self.time_remaining:
            t_lower = self.time_remaining.lower()
            if any(k in t_lower for k in ["atrasad", "expirad", "encerrad", "fechad"]):
                return True
        if self.due_date:
            now = datetime.now(self.due_date.tzinfo) if self.due_date.tzinfo else datetime.now()
            return self.due_date < now
        return False

    @property
    def has_online_submission(self) -> bool:
        """Indica se a atividade aceita submissão online ou se é apenas leitura/em papel ('em branco')."""
        if getattr(self, "activity_type", "") == "quiz":
            return True
        if not self.submission_types:
            if getattr(self, "platform", "") == "canvas":
                return False
            return True
        non_submittable = {"none", "on_paper", "not_graded"}
        return not set(self.submission_types).issubset(non_submittable)

    @property
    def is_actionable_pending(self) -> bool:
        """Indica se a atividade está pendente e pode ser resolvida online."""
        return not self.is_submitted and not self.is_expired and self.has_online_submission

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a atividade em formato compatível com o catálogo do state.json."""
        due_iso = self.due_date.isoformat() if self.due_date else None
        return {
            "id": self.id,
            "course_id": self.course_id,
            "course": self.course_name,
            "course_name": self.course_name,
            "title": self.title,
            "description": self.description,
            "due_date": self.due_date_str or due_iso,
            "due_date_iso": due_iso,
            "is_submitted": self.is_submitted,
            "submission_status": "Enviado" if self.is_submitted else self.submission_status,
            "status": "submitted" if self.is_submitted else "pending",
            "url": self.url,
            "platform": self.platform,
            "activity_type": self.activity_type,
            "points_possible": self.points_possible,
            "time_remaining": self.time_remaining,
            "grade_value": self.grade_value,
            "grading_status": self.grading_status,
            "submission_types": self.submission_types,
            "can_submit": self.has_online_submission,
        }


class LMSAnnouncement(BaseModel):
    """Representa um comunicado ou aviso publicado por um professor ou monitor."""

    id: str = Field(..., description="Identificador único do anúncio no LMS")
    course_id: str = Field(default="", description="ID da disciplina correspondente")
    course_name: str = Field(default="", description="Nome da disciplina")
    title: str = Field(..., description="Título do comunicado")
    message: str = Field(default="", description="Conteúdo textual ou HTML do aviso")
    posted_at: Optional[datetime] = Field(
        default=None,
        description="Data e horário em que foi postado"
    )
    author: str = Field(default="", description="Nome do autor ou remetente")
    url: str = Field(default="", description="URL de acesso ao comunicado")
    platform: Literal["moodle", "canvas"] | str = Field(
        default="moodle",
        description="Plataforma de origem do aviso"
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serializa o comunicado para transmissão ou registro em log."""
        posted_iso = self.posted_at.isoformat() if self.posted_at else None
        return {
            "id": self.id,
            "course_id": self.course_id,
            "course_name": self.course_name,
            "title": self.title,
            "message": self.message,
            "posted_at": posted_iso,
            "author": self.author,
            "url": self.url,
            "platform": self.platform,
        }


# =====================================================================
# Funções utilitárias de conversão a partir de modelos legados do Moodle
# =====================================================================

def from_moodle_course(course: Any) -> LMSCourse:
    """Converte Course legado do Moodle para o modelo neutro LMSCourse."""
    return LMSCourse(
        id=str(getattr(course, "id", "")),
        name=str(getattr(course, "name", "")),
        code=str(getattr(course, "id", "")),
        platform="moodle",
        url=str(getattr(course, "url", "")),
        term=getattr(course, "semester", None)
    )


def from_moodle_assignment(assignment: Any) -> LMSAssignment:
    """Converte Assignment legado do Moodle para o modelo neutro LMSAssignment."""
    due_dt = getattr(assignment, "due_date", None)
    # Se due_date não tiver timezone, normaliza para fuso local
    if due_dt and isinstance(due_dt, datetime) and due_dt.tzinfo is None:
        try:
            local_tz = datetime.now().astimezone().tzinfo
            due_dt = due_dt.replace(tzinfo=local_tz)
        except Exception:
            pass

    return LMSAssignment(
        id=str(getattr(assignment, "id", "")),
        course_id=str(getattr(assignment, "course_id", "")),
        course_name=str(getattr(assignment, "course_name", getattr(assignment, "course", "Disciplina"))),
        title=str(getattr(assignment, "title", "Sem título")),
        description=str(getattr(assignment, "description", "")),
        due_date=due_dt,
        is_submitted=bool(getattr(assignment, "is_submitted", False)),
        url=str(getattr(assignment, "url", "")),
        platform="moodle",
        activity_type=str(getattr(assignment, "activity_type", "assign")),
        points_possible=None,
        submission_status=str(getattr(assignment, "submission_status", "Não enviado")),
        grading_status=getattr(assignment, "grading_status", None),
        grade_value=getattr(assignment, "grade_value", None),
        time_remaining=getattr(assignment, "time_remaining", None),
        due_date_str=getattr(assignment, "due_date_str", None)
    )


def from_moodle_announcement(announcement: Any) -> LMSAnnouncement:
    """Converte CourseAnnouncement legado do Moodle para LMSAnnouncement."""
    posted_dt = None
    date_str = getattr(announcement, "date", "")
    if date_str:
        # Tenta extrair data se possível
        try:
            from src.scraper.moodle_scraper import parse_moodle_date
            dt = parse_moodle_date(date_str)
            if dt and dt.tzinfo is None:
                local_tz = datetime.now().astimezone().tzinfo
                posted_dt = dt.replace(tzinfo=local_tz)
        except Exception:
            pass

    return LMSAnnouncement(
        id=str(getattr(announcement, "id", "")),
        course_id=str(getattr(announcement, "course_id", "")),
        course_name=str(getattr(announcement, "course_name", "")),
        title=str(getattr(announcement, "title", "")),
        message=str(getattr(announcement, "message", "")),
        posted_at=posted_dt,
        author=str(getattr(announcement, "author", "")),
        url=str(getattr(announcement, "url", "")),
        platform="moodle"
    )
