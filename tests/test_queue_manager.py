import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.scheduler.queue_manager import (
    QueueItem,
    QueueTaskStatus,
    QueueTaskType,
    TaskQueueManager,
)


class TestTaskQueueManager(unittest.IsolatedAsyncioTestCase):
    async def test_queue_item_properties(self):
        item = QueueItem(
            task_type=QueueTaskType.RESOLVE_QUIZ,
            title="Quiz 1",
            course="Cálculo I",
            requester="UserA"
        )
        self.assertIsNotNone(item.id)
        self.assertEqual(item.status, QueueTaskStatus.WAITING)
        self.assertEqual(item.elapsed_seconds, 0.0)

    async def test_fifo_sequential_execution(self):
        qm = TaskQueueManager()
        # Reset internal structures for test
        qm._queue = asyncio.Queue()
        qm._waiting_list.clear()
        qm._recent_history.clear()
        qm._running_item = None
        if qm._worker_task and not qm._worker_task.done():
            qm._worker_task.cancel()
        qm._worker_task = None

        execution_log = []

        async def make_task(name: str, delay: float = 0.01):
            async def _coro():
                execution_log.append(f"start_{name}")
                await asyncio.sleep(delay)
                execution_log.append(f"end_{name}")
                return True, f"{name} done"
            return _coro

        item1 = QueueItem(title="Task 1", coro_func=await make_task("T1"))
        item2 = QueueItem(title="Task 2", coro_func=await make_task("T2"))
        item3 = QueueItem(title="Task 3", coro_func=await make_task("T3"))

        pos1 = await qm.enqueue(item1)
        pos2 = await qm.enqueue(item2)
        pos3 = await qm.enqueue(item3)

        self.assertEqual(pos1, 1)
        self.assertEqual(pos2, 2)
        self.assertEqual(pos3, 3)

        # Wait for all tasks to finish
        while qm._running_item is not None or not qm._queue.empty():
            await asyncio.sleep(0.02)

        # Allow worker finally block to finish
        await asyncio.sleep(0.05)

        # Verify strict FIFO sequence
        expected = ["start_T1", "end_T1", "start_T2", "end_T2", "start_T3", "end_T3"]
        self.assertEqual(execution_log, expected)
        self.assertEqual(len(qm._recent_history), 3)
        self.assertEqual(item1.status, QueueTaskStatus.COMPLETED)
        self.assertEqual(item2.status, QueueTaskStatus.COMPLETED)
        self.assertEqual(item3.status, QueueTaskStatus.COMPLETED)

    async def test_error_resilience_does_not_halt_queue(self):
        qm = TaskQueueManager()
        qm._queue = asyncio.Queue()
        qm._waiting_list.clear()
        qm._recent_history.clear()
        qm._running_item = None
        if qm._worker_task and not qm._worker_task.done():
            qm._worker_task.cancel()
        qm._worker_task = None

        log = []

        async def failing_task():
            log.append("fail_start")
            raise ValueError("Erro proposital simulado na tarefa")

        async def successful_task():
            log.append("succ_start")
            return True, "Sucesso"

        item_fail = QueueItem(title="Fail", coro_func=failing_task)
        item_succ = QueueItem(title="Succ", coro_func=successful_task)

        await qm.enqueue(item_fail)
        await qm.enqueue(item_succ)

        while qm._running_item is not None or not qm._queue.empty():
            await asyncio.sleep(0.02)

        await asyncio.sleep(0.05)

        self.assertEqual(log, ["fail_start", "succ_start"])
        self.assertEqual(item_fail.status, QueueTaskStatus.FAILED)
        self.assertIn("Erro proposital simulado", item_fail.error)
        self.assertEqual(item_succ.status, QueueTaskStatus.COMPLETED)

    async def test_build_dashboard_embed(self):
        qm = TaskQueueManager()
        qm._queue = asyncio.Queue()
        qm._waiting_list.clear()
        qm._recent_history.clear()
        qm._running_item = None

        # 1. Idle state
        embed_idle = qm.build_dashboard_embed()
        self.assertIn("Painel da Fila", embed_idle.title)
        self.assertIn("Pronta", embed_idle.description)

        # 2. Running + Waiting state
        running_item = QueueItem(
            title="Questionário 3",
            task_type=QueueTaskType.RESOLVE_QUIZ,
            course="Estatística",
            requester="Aluno"
        )
        qm._running_item = running_item
        waiting_item = QueueItem(
            title="Trabalho Final",
            task_type=QueueTaskType.SUBMIT_ASSIGNMENT,
            course="Computação",
            requester="Professor"
        )
        qm._waiting_list.append(waiting_item)

        embed_active = qm.build_dashboard_embed()
        self.assertIn("Executando", embed_active.description)
        field_names = [f.name for f in embed_active.fields]
        self.assertTrue(any("Em Execução" in fn for fn in field_names))
        self.assertTrue(any("Próximas na Fila" in fn for fn in field_names))


if __name__ == "__main__":
    unittest.main()
