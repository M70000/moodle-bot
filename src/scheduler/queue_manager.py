"""Gerenciador Centralizado de Fila de Tarefas (Task Queue).

Garante execução estritamente sequencial (FIFO) de atividades de IA e
automações do Moodle (Playwright), evitando concorrência de navegadores,
saturação de cotas da API Gemini e conflitos de sessão.

Atualiza um painel persistente em tempo real no canal do Discord configurado.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional
import uuid

import discord
from rich.console import Console

from config.settings import settings

console = Console()


class QueueTaskType(str, Enum):
    RESOLVE_QUIZ = "🧠 Resolvendo Questionário com IA"
    RESOLVE_ASSIGNMENT = "🧠 Resolvendo Tarefa Discursiva com IA"
    REDO_TASK = "🔄 Refazendo Atividade com IA"
    FILL_QUIZ = "📝 Preenchendo Quiz no Moodle (Rascunho)"
    FINALIZE_QUIZ = "🚀 Finalizando e Enviando Quiz no Moodle"
    SUBMIT_ASSIGNMENT = "📄 Submetendo PDF no Moodle"
    PIPELINE_FILL = "📝 Resolução & Preenchimento no Moodle"
    PIPELINE_COMPLETE = "⚡ Resolução & Envio Completo (End-to-End)"
    BATCH_PIPELINE = "📦 Processamento em Lote"
    DAEMON_WATCHER = "🔍 Varredura Automática do Moodle"


class QueueTaskStatus(str, Enum):
    WAITING = "⏳ Aguardando na fila"
    RUNNING = "▶️ Em execução"
    COMPLETED = "✔ Concluído com sucesso"
    FAILED = "❌ Falha na execução"


@dataclass
class QueueItem:
    """Representa um item individual na fila de execução."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:6].upper())
    task_type: QueueTaskType = QueueTaskType.RESOLVE_QUIZ
    title: str = ""
    course: str = ""
    requester: str = "Sistema"
    coro_func: Optional[Callable[[], Coroutine[Any, Any, Any]]] = None
    on_finish: Optional[Callable[[bool, str], Coroutine[Any, Any, Any]]] = None
    enqueued_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    status: QueueTaskStatus = QueueTaskStatus.WAITING
    result_message: str = ""
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskQueueManager:
    """Gerenciador singleton da fila assíncrona sequencial."""

    _instance: Optional["TaskQueueManager"] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TaskQueueManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._waiting_list: List[QueueItem] = []
        self._running_item: Optional[QueueItem] = None
        self._recent_history: List[QueueItem] = []
        self._lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._discord_bot: Optional[Any] = None
        self._dashboard_message: Optional[discord.Message] = None
        self._dashboard_channel_id: int = 0
        self._initialized = True

    def set_bot(self, bot: Any):
        """Define a referência do bot Discord para atualizar o painel do canal da fila."""
        self._discord_bot = bot
        target_ch = settings.DISCORD_QUEUE_CHANNEL_ID or settings.DISCORD_CHANNEL_ID
        self._dashboard_channel_id = int(target_ch) if target_ch else 0

    def is_busy(self) -> bool:
        """Indica se há alguma tarefa em execução no momento."""
        return self._running_item is not None

    def is_busy_except(self, item: QueueItem) -> bool:
        """Indica se há outra tarefa em execução além do item consultado."""
        return self._running_item is not None and self._running_item.id != item.id

    def start_worker(self, bot: Optional[Any] = None):
        """Inicia a corrotina consumidora da fila se ainda não estiver ativa."""
        if bot:
            self.set_bot(bot)

        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
            console.print("[green]✔ Worker da Fila Centralizada de Tarefas iniciado com sucesso![/green]")

    async def enqueue(self, item: QueueItem) -> int:
        """Insere um item na fila de execução. Retorna a posição na fila (1-indexed)."""
        if not self._worker_task or self._worker_task.done():
            self.start_worker(self._discord_bot)

        async with self._lock:
            self._waiting_list.append(item)
            await self._queue.put(item)
            position = len(self._waiting_list)
            console.print(
                f"[cyan]📥 Tarefa adicionada à fila [{item.id}]: [bold]{item.title}[/bold] "
                f"({item.task_type.value}) - Posição: #{position}[/cyan]"
            )

        # Atualiza o painel do Discord assincronamente
        asyncio.create_task(self.update_discord_dashboard())
        return position

    async def _worker_loop(self):
        """Loop contínuo que processa cada tarefa da fila sequencialmente."""
        while True:
            item = await self._queue.get()

            async with self._lock:
                if item in self._waiting_list:
                    self._waiting_list.remove(item)
                self._running_item = item
                item.status = QueueTaskStatus.RUNNING
                item.started_at = datetime.now()

            console.print(
                f"\n[bold green]▶ [FILA] Iniciando tarefa [{item.id}]: {item.title} "
                f"({item.task_type.value})[/bold green]"
            )
            await self.update_discord_dashboard()

            success = False
            message = ""
            try:
                if item.coro_func:
                    res = await item.coro_func()
                    if isinstance(res, tuple) and len(res) == 2:
                        success, message = res
                    else:
                        success = True
                        message = str(res) if res is not None else "Executado com sucesso"
                else:
                    success = True
                    message = "Tarefa concluída (sem corrotina associada)"

                item.status = QueueTaskStatus.COMPLETED
                item.result_message = message
            except Exception as exc:
                item.status = QueueTaskStatus.FAILED
                item.error = str(exc)
                item.result_message = f"Erro: {exc}"
                console.print(f"[bold red]❌ [FILA] Erro na execução de [{item.id}] {item.title}: {exc}[/bold red]")
            finally:
                item.finished_at = datetime.now()
                async with self._lock:
                    self._running_item = None
                    self._recent_history.append(item)
                    if len(self._recent_history) > 5:
                        self._recent_history = self._recent_history[-5:]

                if item.on_finish:
                    try:
                        await item.on_finish(success, message)
                    except Exception as fin_err:
                        console.print(f"[yellow]Aviso no callback on_finish de [{item.id}]: {fin_err}[/yellow]")

                await self.update_discord_dashboard()
                self._queue.task_done()

    def get_snapshot(self) -> tuple[Optional[QueueItem], List[QueueItem], List[QueueItem]]:
        """Retorna (running_item, waiting_list, recent_history)."""
        return self._running_item, list(self._waiting_list), list(self._recent_history)

    def build_dashboard_embed(self) -> discord.Embed:
        """Monta o Embed do painel ao vivo com visual rico e limpo."""
        running = self._running_item
        waiting = list(self._waiting_list)
        history = list(self._recent_history)

        total_pending = len(waiting)
        is_active = (running is not None)

        if is_active:
            color = discord.Color.gold()
            status_header = f"🟡 **Executando Tarefa** ({total_pending} na fila de espera)"
        elif waiting:
            color = discord.Color.blue()
            status_header = f"🔵 **Preparando** ({total_pending} na fila)"
        else:
            color = discord.Color.green()
            status_header = "🟢 **Fila Ociosa / Pronta para Novas Tarefas**"

        embed = discord.Embed(
            title="🔄 Painel da Fila de Execução (Moodle Bot)",
            description=f"Status da Fila: {status_header}\n*Execução estritamente sequencial para segurança do Moodle e IA.*",
            color=color,
            timestamp=datetime.now()
        )

        # 1. Item em Execução
        if running:
            elapsed = int(running.elapsed_seconds)
            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins:02d}:{secs:02d}"
            req_time = running.started_at.strftime("%H:%M:%S") if running.started_at else "Agora"
            embed.add_field(
                name="▶️ Em Execução Agora",
                value=(
                    f"• **Operação:** `{running.task_type.value}`\n"
                    f"• **Atividade:** **{running.title}**\n"
                    f"• **Disciplina:** {running.course or 'Geral'}\n"
                    f"• **Solicitante:** {running.requester} | **Início:** {req_time} (⏱️ `{elapsed_str}` decorridos)"
                ),
                inline=False
            )
        else:
            embed.add_field(
                name="▶️ Em Execução Agora",
                value="*Nenhuma tarefa em execução no momento. Sistema pronto.*",
                inline=False
            )

        # 2. Próximas Tarefas na Fila
        if waiting:
            lines = []
            for idx, item in enumerate(waiting[:8], start=1):
                wait_time = int((datetime.now() - item.enqueued_at).total_seconds())
                w_mins, w_secs = divmod(wait_time, 60)
                lines.append(
                    f"**#{idx}** `[{item.id}]` {item.task_type.value}\n"
                    f"   ↳ **{item.title}** ({item.requester} • aguardando há {w_mins}m{w_secs:02d}s)"
                )
            if len(waiting) > 8:
                lines.append(f"*... e mais {len(waiting) - 8} tarefa(s) na fila.*")
            embed.add_field(
                name=f"📋 Próximas na Fila ({len(waiting)})",
                value="\n".join(lines),
                inline=False
            )
        else:
            embed.add_field(
                name="📋 Próximas na Fila",
                value="*Nenhuma tarefa aguardando na fila de espera.*",
                inline=False
            )

        # 3. Histórico Recente
        if history:
            h_lines = []
            for item in reversed(history[-4:]):
                icon = "✔" if item.status == QueueTaskStatus.COMPLETED else "❌"
                fin_time = item.finished_at.strftime("%H:%M:%S") if item.finished_at else ""
                dur = f"{int(item.elapsed_seconds)}s"
                h_lines.append(
                    f"{icon} `[{fin_time}]` **{item.title[:35]}**\n"
                    f"   ↳ *{item.task_type.value}* ({dur})"
                )
            embed.add_field(
                name="🏁 Concluídos Recentemente",
                value="\n".join(h_lines),
                inline=False
            )

        embed.set_footer(text="Moodle Bot UFMG • Fila Centralizada")
        return embed

    async def update_discord_dashboard(self):
        """Envia ou atualiza a mensagem fixa do painel no canal exclusivo da fila."""
        if not self._discord_bot:
            return

        target_ch_id = self._dashboard_channel_id or settings.DISCORD_QUEUE_CHANNEL_ID or settings.DISCORD_CHANNEL_ID
        if not target_ch_id or target_ch_id == 0:
            return

        channel = self._discord_bot.get_channel(int(target_ch_id))
        if not channel:
            try:
                channel = await self._discord_bot.fetch_channel(int(target_ch_id))
            except Exception:
                return

        embed = self.build_dashboard_embed()

        try:
            if self._dashboard_message:
                try:
                    await self._dashboard_message.edit(embed=embed)
                    return
                except Exception:
                    self._dashboard_message = None

            # Envia nova mensagem caso não exista ou tenha sido apagada
            self._dashboard_message = await channel.send(embed=embed)
        except Exception as exc:
            console.print(f"[yellow]Aviso ao atualizar painel da fila no Discord: {exc}[/yellow]")


# Instância global do gerenciador
queue_manager = TaskQueueManager()
