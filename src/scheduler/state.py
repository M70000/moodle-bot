"""Gerenciamento de persistência de estado do Moodle AI Assistant.

Armazena em storage/state.json o histórico de tarefas analisadas, rascunhos,
adiamentos, cancelamentos e confirmações de envio.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

STATE_FILE = Path("storage/state.json")


class DaemonState:
    """Gerencia a persistência de estado das tarefas e decisões do usuário."""

    def __init__(self, file_path: Path = STATE_FILE):
        self.file_path = file_path
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.file_path.exists():
            try:
                return json.loads(self.file_path.read_text(encoding="utf-8"))
            except Exception:
                return {"assignments": {}}
        return {"assignments": {}}

    def save(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_assignment(self, assign_id: str) -> Optional[Dict[str, Any]]:
        return self.data.get("assignments", {}).get(assign_id)

    def register_assignment(self, assignment, draft_path: Optional[str] = None):
        if "assignments" not in self.data:
            self.data["assignments"] = {}

        existing = self.data["assignments"].get(assignment.id, {})
        existing.update({
            "id": assignment.id,
            "title": assignment.title,
            "course": assignment.course_name,
            "url": assignment.url,
            "due_date": assignment.due_date_str,
            "time_remaining": assignment.time_remaining,
            "activity_type": getattr(assignment, "activity_type", "assign"),
            "submission_status": getattr(assignment, "submission_status", "Não enviado"),
            "grade_value": getattr(assignment, "grade_value", None),
            "has_grade": getattr(assignment, "has_grade", False),
            "is_submitted": assignment.is_submitted,
            "draft_path": draft_path or existing.get("draft_path"),
            "status": existing.get("status", "pending_review"),
            "alert_15m_sent": existing.get("alert_15m_sent", False),
            "alert_5m_sent": existing.get("alert_5m_sent", False),
            "alert_2m_sent": existing.get("alert_2m_sent", False),
            "alert_1m_sent": existing.get("alert_1m_sent", False),
            "updated_at": datetime.now().isoformat()
        })
        self.data["assignments"][assignment.id] = existing
        self.save()

    def postpone_assignment(self, assign_id: str, hours: int = 1) -> str:
        """Adia as notificações da tarefa pelo tempo especificado."""
        if "assignments" not in self.data:
            self.data["assignments"] = {}
        item = self.data["assignments"].setdefault(assign_id, {})
        until = datetime.now() + timedelta(hours=hours)
        item["status"] = "postponed"
        item["postponed_until"] = until.isoformat()
        item["updated_at"] = datetime.now().isoformat()
        self.save()
        return until.strftime("%H:%M")

    def cancel_assignment(self, assign_id: str):
        """Cancela o monitoramento e qualquer envio da tarefa."""
        if "assignments" not in self.data:
            self.data["assignments"] = {}
        item = self.data["assignments"].setdefault(assign_id, {})
        item["status"] = "cancelled"
        item["updated_at"] = datetime.now().isoformat()
        self.save()

    def mark_submitted(self, assign_id: str):
        """Registra a submissão concluída no Moodle."""
        if "assignments" not in self.data:
            self.data["assignments"] = {}
        item = self.data["assignments"].setdefault(assign_id, {})
        item["status"] = "submitted"
        item["is_submitted"] = True
        item["updated_at"] = datetime.now().isoformat()
        self.save()

    def is_postponed(self, assign_id: str) -> bool:
        """Verifica se a tarefa está dentro do período de adiamento."""
        item = self.get_assignment(assign_id)
        if not item or item.get("status") != "postponed":
            return False

        until_str = item.get("postponed_until")
        if not until_str:
            return False

        try:
            until_dt = datetime.fromisoformat(until_str)
            return datetime.now() < until_dt
        except Exception:
            return False

    def is_announcement_seen(self, announcement_id: str) -> bool:
        """Verifica se o aviso já foi registrado anteriormente."""
        return str(announcement_id) in self.data.get("announcements", {})

    def mark_announcement_seen(self, announcement):
        """Registra um aviso da turma como notificado/conhecido."""
        if "announcements" not in self.data:
            self.data["announcements"] = {}

        self.data["announcements"][str(announcement.id)] = {
            "id": str(announcement.id),
            "course_id": announcement.course_id,
            "course_name": announcement.course_name,
            "title": announcement.title,
            "author": getattr(announcement, "author", ""),
            "date": getattr(announcement, "date", ""),
            "url": announcement.url,
            "notified_at": datetime.now().isoformat()
        }
        self.save()

    def get_known_announcement_ids(self) -> set:
        """Retorna o conjunto de IDs de avisos já conhecidos."""
        return set(self.data.get("announcements", {}).keys())
