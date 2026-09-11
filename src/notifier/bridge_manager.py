"""Módulo de Gestão da Ponte Nuvem (Render Hub <-> Desktop Runner).

Permite que ações de submissão (Aprovar PDF, Preencher Quiz, Finalizar Quiz)
acionadas por botões no Discord rodando na Nuvem (Render) sejam despachadas e
executadas com segurança no PC local do estudante (onde reside o login da UFMG).
"""

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional


class CloudBridgeManager:
    """Gerencia a fila em memória de despachos e conclusões de submissão."""

    def __init__(self):
        self._pending_tasks: Dict[str, Dict[str, Any]] = {}
        self._completed_tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def dispatch_action(
        self,
        action: str,  # 'approve_assign', 'fill_quiz', 'finalize_quiz'
        assignment_id: str,
        assignment_url: str,
        channel_id: str,
        message_id: str,
        requester: str,
        title: str = "",
        course: str = "",
        file_to_submit: Optional[str] = None,
        structured_answers: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Enfileira uma solicitação de submissão para ser consumida pelo desktop."""
        task_id = f"bridge_{uuid.uuid4().hex[:8]}"
        payload = {
            "task_id": task_id,
            "action": action,
            "assignment_id": str(assignment_id),
            "assignment_url": str(assignment_url or ""),
            "channel_id": str(channel_id),
            "message_id": str(message_id),
            "requester": requester,
            "title": title or f"Tarefa {assignment_id}",
            "course": course or "Geral",
            "file_to_submit": file_to_submit,
            "structured_answers": structured_answers or {},
            "status": "pending",
            "created_at": time.time(),
        }
        async with self._lock:
            self._pending_tasks[task_id] = payload
        return task_id

    async def get_pending_tasks(self, channel_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retorna as tarefas pendentes, filtrando opcionalmente por canal privado."""
        async with self._lock:
            tasks = []
            for t in self._pending_tasks.values():
                if t["status"] == "pending":
                    if not channel_id or str(t.get("channel_id")) == str(channel_id):
                        tasks.append(t)
            return tasks

    async def mark_in_progress(self, task_id: str):
        """Marca uma tarefa como em execução pelo desktop."""
        async with self._lock:
            if task_id in self._pending_tasks:
                self._pending_tasks[task_id]["status"] = "in_progress"

    async def complete_task(self, task_id: str, success: bool, message: str) -> Optional[Dict[str, Any]]:
        """Finaliza uma tarefa e armazena o resultado para feedback no Discord."""
        async with self._lock:
            if task_id in self._pending_tasks:
                task = self._pending_tasks.pop(task_id)
                task["status"] = "completed" if success else "failed"
                task["success"] = success
                task["result_message"] = message
                task["completed_at"] = time.time()
                self._completed_tasks[task_id] = task
                return task
            return None


cloud_bridge = CloudBridgeManager()
