"""Módulo Daemon / Scheduler em segundo plano do Moodle AI Assistant.

Coordena a varredura contínua de novas tarefas e materiais, dispara a resolução
com Gemini IA, notifica para aprovação no Discord e gerencia o sistema de alertas
de prazo (sem submissão automática indesejada).
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from rich.console import Console
from rich.panel import Panel

from config.settings import settings
from src.auth.moodle_auth import MoodleAuth
from src.notifier.discord_bot import MoodleDiscordNotifier
from src.scraper.moodle_scraper import Assignment, CourseAnnouncement, MoodleScraper, sanitize_filename
from src.scheduler.state import DaemonState
from src.solver.gemini_solver import GeminiSolver

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


class MoodleDaemon:
    """Daemon principal que orquestra a inteligência em segundo plano."""

    def __init__(self):
        self.auth = MoodleAuth()
        self.scraper = MoodleScraper(auth=self.auth)
        self.solver = GeminiSolver()
        self.notifier = MoodleDiscordNotifier()
        self.state = DaemonState()
        self.scheduler = AsyncIOScheduler()
        self._running = False

    async def run_cycle(self):
        """Executa um ciclo completo de verificação, resolução e notificação."""
        console.rule("[bold cyan]Iniciando Ciclo de Varredura do Moodle[/bold cyan]")
        try:
            # 1. Valida se a sessão continua ativa
            valid, user = await self.auth.validate_session()
            if not valid:
                console.print("[bold red]Sessão expirada. Tentando re-autenticar...[/bold red]")
                auth_success = await self.auth.ensure_authenticated()
                if not auth_success:
                    console.print("[red]Não foi possível restabelecer a sessão do Moodle.[/red]")
                    return

            known_ann_ids = self.state.get_known_announcement_ids()
            first_ann_run = len(known_ann_ids) == 0

            # 2. Executa a varredura das disciplinas, tarefas e comunicados
            courses, assignments, announcements = await self.scraper.scan_all(
                sync_materials=True,
                sync_announcements=True,
                known_announcement_ids=known_ann_ids
            )

            # 3. Processa comunicados da turma dos professores
            for ann in announcements:
                if not self.state.is_announcement_seen(ann.id):
                    # Na primeira execução, notifica avisos recentes (ex: do mês corrente ou últimos dias)
                    is_recent = any(m in ann.date.lower() for m in ["set", "out", "nov", "dez", "hoje", "ontem"]) if ann.date else True
                    if first_ann_run and not is_recent:
                        self.state.mark_announcement_seen(ann)
                        continue

                    console.print(f"[bold yellow]📢 NOVO AVISO DETECTADO:[/bold yellow] {ann.title} ({ann.course_name})")
                    await self.notifier.send_course_announcement(ann)
                    self.state.mark_announcement_seen(ann)

            # 4. Processa cada atividade
            for assign in assignments:
                assign_state = self.state.get_assignment(assign.id)

                # Monitor de Notas & Feedback: verifica se o professor lançou a avaliação
                if assign.has_grade and assign.grade_value:
                    already_notified_grade = assign_state and assign_state.get("grade_notified", False)
                    if not already_notified_grade:
                        console.print(
                            f"[bold green]🎉 NOTA DETECTADA no Moodle:[/bold green] "
                            f"{assign.title} -> {assign.grade_value}"
                        )
                        await self.notifier.send_grade_notification(
                            course_name=assign.course_name,
                            assignment_title=assign.title,
                            grade=assign.grade_value,
                            feedback=assign.feedback_comments,
                            graded_by=assign.graded_by
                        )
                        if assign_state:
                            assign_state["grade_notified"] = True
                            assign_state["grade_value"] = assign.grade_value
                            assign_state["feedback_comments"] = assign.feedback_comments
                            self.state.save()

                # Se a tarefa não é acionável (já enviada ou prazo vencido no passado), ignora
                if not assign.is_actionable_pending:
                    self.state.register_assignment(assign)
                    continue

                # Se o usuário cancelou essa tarefa pelo Discord, respeita e não processa
                if assign_state and assign_state.get("status") == "cancelled":
                    console.print(f"[dim]Tarefa {assign.title} marcada como cancelada pelo usuário. Ignorando.[/dim]")
                    continue

                # Se for questionário online (quiz), registra no catálogo para acompanhamento em /tarefas (resolução sob demanda)
                if getattr(assign, "activity_type", "assign") == "quiz":
                    self.state.register_assignment(assign)
                    continue

                # Se a tarefa foi adiada pelo usuário e o tempo ainda não passou, pula
                if self.state.is_postponed(assign.id):
                    console.print(f"[dim]Tarefa {assign.title} adiada pelo usuário. Notificações temporariamente em pausa.[/dim]")
                    continue

                # Se a tarefa é nova e ainda não possui rascunho gerado
                has_draft = assign_state and assign_state.get("draft_path")
                if not has_draft:
                    console.print(
                        f"[bold yellow]Nova tarefa pendente identificada:[/bold yellow] "
                        f"{assign.title} ({assign.course_name})"
                    )

                    # Gera o rascunho e PDF acadêmico com o Gemini
                    if self.solver.client:
                        try:
                            draft = await self.solver.solve_assignment(assign)
                            self.state.register_assignment(assign, draft_path=str(draft.output_path))

                            # Envia para revisão humana no Discord
                            if self.notifier.token and self.notifier.channel_id:
                                await self.notifier.send_assignment_review(assign, draft)
                            else:
                                console.print("[yellow]Discord não configurado: rascunho gerado e salvo em disco.[/yellow]")

                        except Exception as sol_err:
                            console.print(f"[red]Erro ao resolver tarefa {assign.title}: {sol_err}[/red]")
                    else:
                        console.print("[yellow]Gemini não configurado: rascunho não gerado.[/yellow]")
                else:
                    self.state.register_assignment(assign)

            console.print("[green]✔ Ciclo de varredura concluído com sucesso.[/green]")

        except Exception as e:
            console.print(f"[bold red]Erro inesperado no ciclo de varredura: {e}[/bold red]")

    async def timeline_watcher(self):
        """Monitora a cada minuto a contagem regressiva e dispara alertas progressivos (Spam de Prazo)."""
        for assign_id, item in list(self.state.data.get("assignments", {}).items()):
            # Ignora tarefas já submetidas, canceladas ou que já foram descartadas
            if item.get("is_submitted") or item.get("status") in ["cancelled", "submitted"]:
                continue

            # Se estiver adiada no momento, respeita a pausa
            if self.state.is_postponed(assign_id):
                continue

            time_str = (item.get("time_remaining") or "").lower()
            title = item.get("title", "Atividade")
            course = item.get("course", "")

            # 1. Alerta de 15 minutos
            if "15 minuto" in time_str or "14 minuto" in time_str:
                if not item.get("alert_15m_sent"):
                    console.print(f"[yellow]Disparando alerta de 15 min para: {title}[/yellow]")
                    await self.notifier.send_countdown_alert(title, course, minutes_remaining=15)
                    item["alert_15m_sent"] = True
                    self.state.save()

            # 2. Alerta de 5 minutos (Alta prioridade com ping)
            is_close_5m = any(p in time_str for p in ["5 minuto", "4 minuto", "3 minuto"])
            if is_close_5m and not item.get("alert_5m_sent"):
                console.print(f"[bold red]Disparando alerta de 5 min para: {title}[/bold red]")
                await self.notifier.send_countdown_alert(title, course, minutes_remaining=5)
                item["alert_5m_sent"] = True
                self.state.save()

            # 3. Alerta de 2 minutos
            if "2 minuto" in time_str and not item.get("alert_2m_sent"):
                console.print(f"[bold red]Disparando alerta de 2 min para: {title}[/bold red]")
                await self.notifier.send_countdown_alert(title, course, minutes_remaining=2)
                item["alert_2m_sent"] = True
                self.state.save()

            # 4. Alerta de Último Minuto (<60s)
            is_close_1m = "1 minuto" in time_str or "segundos" in time_str
            if is_close_1m and not item.get("alert_1m_sent"):
                console.print(f"[bold red]Disparando ALERTA MÁXIMO DE ÚLTIMO MINUTO para: {title}[/bold red]")
                # NUNCA submete sozinho - em vez disso alerta com máxima urgência
                await self.notifier.send_countdown_alert(title, course, minutes_remaining=1)
                item["alert_1m_sent"] = True
                self.state.save()

    async def start(self):
        """Inicia o daemon e agenda os jobs em segundo plano."""
        self._running = True

        console.print(
            Panel.fit(
                "[bold cyan]Moodle AI Assistant Daemon (UFMG)[/bold cyan]\n\n"
                f"• URL: [underline]{settings.MOODLE_BASE_URL}[/underline]\n"
                f"• Intervalo de varredura: [bold]{settings.CHECK_INTERVAL_MINUTES} minutos[/bold]\n"
                f"• IA: [bold]{settings.GEMINI_MODEL}[/bold]\n"
                "• Submissão: [bold green]Estritamente sob aprovação humana[/bold green]\n"
                "• Alertas de prazo: [bold yellow]Contagem regressiva intensiva (15m, 5m, 2m, 1m)[/bold yellow]\n"
                "• Pressione [bold]Ctrl+C[/bold] para encerrar.",
                title="[bold green]Daemon Ativo e Seguro[/bold green]",
                border_style="green"
            )
        )

        if settings.DISCORD_BOT_TOKEN and settings.DISCORD_BOT_TOKEN != "seu_discord_bot_token_aqui":
            from src.notifier.discord_bot import bot
            console.print("[cyan]Conectando Bot do Discord para habilitar comandos...[/cyan]")
            try:
                await bot.login(settings.DISCORD_BOT_TOKEN)
                asyncio.create_task(bot.connect())
                await asyncio.wait_for(bot.wait_until_ready(), timeout=12.0)
                console.print("[green]✔ Bot do Discord conectado e pronto para uso![/green]")
            except Exception as bot_err:
                console.print(f"[yellow]Nota ao conectar bot do Discord: {bot_err}[/yellow]")

        # 1. Executa um ciclo imediato
        await self.run_cycle()

        # 2. Agenda a varredura periódica
        self.scheduler.add_job(
            self.run_cycle,
            "interval",
            minutes=settings.CHECK_INTERVAL_MINUTES,
            id="moodle_sync_job"
        )

        # 3. Agenda o monitor de timeline / contagem regressiva (a cada 60s)
        self.scheduler.add_job(
            self.timeline_watcher,
            "interval",
            seconds=60,
            id="timeline_watcher_job"
        )

        self.scheduler.start()

        while self._running:
            await asyncio.sleep(1)

    def stop(self):
        """Para o daemon com segurança."""
        self._running = False
        if self.scheduler.running:
            self.scheduler.shutdown()
        console.print("[bold yellow]Daemon encerrado com segurança.[/bold yellow]")


async def main():
    parser = argparse.ArgumentParser(description="Moodle AI Assistant Background Daemon")
    parser.add_argument("--once", action="store_true", help="Executa apenas um ciclo e encerra")
    args = parser.parse_args()

    daemon = MoodleDaemon()

    if args.once:
        if settings.DISCORD_BOT_TOKEN and settings.DISCORD_BOT_TOKEN != "seu_discord_bot_token_aqui":
            from src.notifier.discord_bot import bot
            asyncio.create_task(bot.start(settings.DISCORD_BOT_TOKEN))
            try:
                await asyncio.wait_for(bot.wait_until_ready(), timeout=10.0)
            except Exception:
                pass

        await daemon.run_cycle()

        if settings.DISCORD_BOT_TOKEN:
            from src.notifier.discord_bot import bot
            if not bot.is_closed():
                await bot.close()
        sys.exit(0)

    try:
        await daemon.start()
    except (KeyboardInterrupt, SystemExit):
        daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
