"""Módulo de Gestão da Ponte Nuvem (Render Hub <-> Desktop Runner).

Permite que ações de submissão (Aprovar PDF, Preencher Quiz, Finalizar Quiz)
acionadas por botões no Discord rodando na Nuvem (Render) sejam despachadas e
executadas com segurança no PC local do estudante (onde reside o login da UFMG).

Também gerencia:
- Lista de disciplinas publicadas pelo desktop (para autocomplete no relay mode)
- Status de presença do desktop runner (online/offline)
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
        self._task_events: Dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

        # Presença, cursos, tarefas e materiais publicados pelo desktop runner ou Discord
        self._published_courses: List[str] = []
        self._published_assignments: Dict[str, Dict[str, Any]] = {}
        self._custom_materials: List[Dict[str, Any]] = []
        self._desktop_last_seen: float = 0.0  # Unix timestamp
        self._DESKTOP_TIMEOUT_SECONDS = 300   # 5 min sem heartbeat → offline

        # Isolamento estrito por usuário/canal (Multi-User Bridge)
        self._published_courses_by_channel: Dict[str, List[str]] = {}
        self._published_assignments_by_channel: Dict[str, Dict[str, Any]] = {}
        self._custom_materials_by_channel: Dict[str, List[Dict[str, Any]]] = {}
        self._desktop_last_seen_by_channel: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Desktop presence & published state (courses + assignments + materials)
    # ------------------------------------------------------------------

    def _is_valid_material(self, material: Dict[str, Any]) -> bool:
        c = material.get("course")
        fn = material.get("filename")
        att = str(material.get("attachment_url", ""))
        uploader = str(material.get("uploader", ""))
        if not c or not fn or not att:
            return False
        # Descarta objetos de MagicMock acidentalmente vazados
        if "magicmock" in uploader.lower():
            return False
        return True

    def _upsert_material_unlocked(self, material: Dict[str, Any]) -> None:
        if not self._is_valid_material(material):
            return
        c = material.get("course")
        fn = material.get("filename")
        for existing in self._custom_materials:
            if existing.get("course") == c and existing.get("filename") == fn:
                existing.update(material)
                return
        self._custom_materials.append(dict(material))

    async def register_material(self, material: Dict[str, Any], channel_id: Optional[str] = None) -> None:
        """Registra ou atualiza um material customizado na ponte nuvem."""
        async with self._lock:
            self._upsert_material_unlocked(material)
            if channel_id:
                cid = str(channel_id).strip()
                if cid not in self._custom_materials_by_channel:
                    self._custom_materials_by_channel[cid] = []
                c = material.get("course")
                fn = material.get("filename")
                found = False
                for ex in self._custom_materials_by_channel[cid]:
                    if ex.get("course") == c and ex.get("filename") == fn:
                        ex.update(material)
                        found = True
                        break
                if not found:
                    self._custom_materials_by_channel[cid].append(dict(material))

    async def get_custom_materials(self, channel_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retorna todos os materiais customizados conhecidos na ponte (opcionalmente filtrados por canal)."""
        async with self._lock:
            if channel_id and str(channel_id) in self._custom_materials_by_channel:
                return [dict(m) for m in self._custom_materials_by_channel[str(channel_id)] if self._is_valid_material(m)]
            return [dict(m) for m in self._custom_materials if self._is_valid_material(m)]

    async def publish_state(
        self,
        courses: Optional[List[str]] = None,
        assignments: Optional[Dict[str, Any]] = None,
        custom_materials: Optional[List[Dict[str, Any]]] = None,
        channel_id: Optional[str] = None
    ) -> None:
        """Recebe e armazena disciplinas, catálogo de tarefas e materiais do desktop runner com isolamento por canal."""
        async with self._lock:
            now_ts = time.time()
            cid = str(channel_id).strip() if channel_id else None

            if cid:
                if courses is not None:
                    self._published_courses_by_channel[cid] = list(courses)
                if assignments is not None:
                    self._published_assignments_by_channel[cid] = dict(assignments)
                if custom_materials is not None:
                    self._custom_materials_by_channel[cid] = [
                        dict(m) for m in custom_materials if self._is_valid_material(m)
                    ]
                self._desktop_last_seen_by_channel[cid] = now_ts

            if courses is not None:
                self._published_courses = list(courses)
            if assignments is not None:
                self._published_assignments = dict(assignments)
            if custom_materials is not None:
                clean_mats = []
                for mat in custom_materials:
                    if self._is_valid_material(mat):
                        clean_mats.append(dict(mat))
                self._custom_materials = clean_mats
            self._desktop_last_seen = now_ts

    async def publish_courses(self, courses: List[str], channel_id: Optional[str] = None) -> None:
        """Recebe e armazena a lista de disciplinas publicada pelo desktop."""
        await self.publish_state(courses=courses, channel_id=channel_id)

    async def get_published_courses(self, channel_id: Optional[str] = None) -> List[str]:
        """Retorna as disciplinas publicadas pelo desktop (por canal ou global)."""
        async with self._lock:
            if channel_id and str(channel_id) in self._published_courses_by_channel:
                return list(self._published_courses_by_channel[str(channel_id)])
            return list(self._published_courses)

    async def get_published_assignments(self, channel_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Retorna as tarefas catalogadas publicadas pelo desktop (por canal ou global)."""
        async with self._lock:
            if channel_id and str(channel_id) in self._published_assignments_by_channel:
                return dict(self._published_assignments_by_channel[str(channel_id)])
            return dict(self._published_assignments)

    async def is_desktop_online(self, channel_id: Optional[str] = None) -> bool:
        """Retorna True se o desktop publicou presença nos últimos 5 minutos."""
        async with self._lock:
            if channel_id and str(channel_id) in self._desktop_last_seen_by_channel:
                return (time.time() - self._desktop_last_seen_by_channel[str(channel_id)]) < self._DESKTOP_TIMEOUT_SECONDS
            if not self._desktop_last_seen:
                return False
            return (time.time() - self._desktop_last_seen) < self._DESKTOP_TIMEOUT_SECONDS

    async def desktop_status(self) -> Dict[str, Any]:
        """Retorna dicionário com status do desktop runner para o healthcheck."""
        async with self._lock:
            online = bool(
                self._desktop_last_seen
                and (time.time() - self._desktop_last_seen) < self._DESKTOP_TIMEOUT_SECONDS
            )
            return {
                "online": online,
                "last_seen": self._desktop_last_seen or None,
                "courses_count": len(self._published_courses),
                "assignments_count": len(self._published_assignments),
                "custom_materials_count": len(self._custom_materials),
            }

    # ------------------------------------------------------------------
    # Task queue
    # ------------------------------------------------------------------

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
        platform: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Enfileira uma solicitação de submissão para ser consumida pelo desktop."""
        task_id = f"bridge_{uuid.uuid4().hex[:8]}"
        url_str = str(assignment_url or "").lower()
        detected_platform = platform or ("canvas" if "canvas" in url_str or "instructure.com" in url_str else "moodle")
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
            "platform": detected_platform,
            "status": "pending",
            "created_at": time.time(),
        }
        if kwargs:
            payload.update(kwargs)
        event = asyncio.Event()
        async with self._lock:
            self._pending_tasks[task_id] = payload
            self._task_events[task_id] = event
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
        event = None
        async with self._lock:
            event = self._task_events.pop(task_id, None)
            if task_id in self._pending_tasks:
                task = self._pending_tasks.pop(task_id)
                task["status"] = "completed" if success else "failed"
                task["success"] = success
                task["result_message"] = message
                task["completed_at"] = time.time()
                self._completed_tasks[task_id] = task
                if event:
                    event.set()
                return task
            elif event:
                event.set()
            return None

    async def wait_for_task(self, task_id: str, timeout: float = 300.0) -> Optional[Dict[str, Any]]:
        """Aguarda até que o desktop runner conclua a tarefa ou estoure o timeout."""
        async with self._lock:
            if task_id in self._completed_tasks:
                return dict(self._completed_tasks[task_id])
            event = self._task_events.get(task_id)

        if not event:
            return None

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            async with self._lock:
                return dict(self._completed_tasks.get(task_id, {}))
        except asyncio.TimeoutError:
            return None

cloud_bridge = CloudBridgeManager()

