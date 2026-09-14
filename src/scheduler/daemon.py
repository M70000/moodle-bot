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
from src.solver.ai_solver import AISolver

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

    def __init__(self, enable_tray: bool = True):
        self.auth = MoodleAuth()
        self.scraper = MoodleScraper(auth=self.auth)
        self.solver = AISolver()  # Multi-provider BYOK: Gemini/Claude/DeepSeek
        self.notifier = MoodleDiscordNotifier()
        self.state = DaemonState()
        self.scheduler = AsyncIOScheduler()
        self._running = False
        self._session_expired_alerted = False
        self._is_reauthenticating = False
        self.enable_tray = enable_tray
        self.tray = None


    async def _auto_relogin_flow(self, force_interactive: bool = False):
        """Executa o fluxo de renovação de sessão conforme o modo de autenticação.

        - Se AUTH_MODE == 'credentials' e não forçado: executa login automático em background via credenciais.
        - Se AUTH_MODE == 'cookies' ou forçado: abre o navegador interativo no desktop.
        """
        if self._is_reauthenticating:
            return
        self._is_reauthenticating = True
        try:
            auth_mode = getattr(settings, "AUTH_MODE", "cookies").lower()
            if auth_mode == "credentials" and not force_interactive:
                console.print("[bold cyan]🔄 Sessão do Moodle expirada: Tentando login automático com credenciais (headless)...[/bold cyan]")
                success, user_or_err = await self.auth.login_with_credentials(headless=True)
                if success:
                    console.print("[bold green]✔ Sessão renovada automaticamente com sucesso![/bold green]")
                    self._session_expired_alerted = False
                    if self.notifier.token and self.notifier.channel_id:
                        await self.notifier.send_session_renewed_notification(user_name=user_or_err)
                else:
                    console.print(f"[bold red]❌ Falha na renovação automática via credenciais: {user_or_err}[/bold red]")
                    if not self._session_expired_alerted:
                        self._session_expired_alerted = True
                        if self.notifier.token and self.notifier.channel_id:
                            await self.notifier.send_session_expired_alert(
                                browser_opened=False,
                                custom_details=f"Falha no login automático com credenciais: {user_or_err}"
                            )
            else:
                console.print("[bold cyan]🔄 Abrindo navegador no desktop para login interativo no MinhaUFMG...[/bold cyan]")
                success = await self.auth.interactive_login(headless=False)
                if success:
                    console.print("[bold green]✔ Sessão renovada com sucesso pelo navegador interativo![/bold green]")
                    self._session_expired_alerted = False
                    valid, user = await self.auth.validate_session()
                    if self.notifier.token and self.notifier.channel_id:
                        await self.notifier.send_session_renewed_notification(user_name=user)
                else:
                    console.print("[yellow]Aviso: Janela de login interativo fechada sem autenticação confirmada.[/yellow]")
        except Exception as err:
            console.print(f"[red]Erro durante renovação de login: {err}[/red]")
        finally:
            self._is_reauthenticating = False

    async def run_cycle(self):
        """Executa um ciclo completo de verificação, resolução e notificação."""
        console.rule("[bold cyan]Iniciando Ciclo de Varredura do Moodle[/bold cyan]")
        try:
            # 1. Valida se a sessão continua ativa
            valid, user = await self.auth.validate_session()
            if not valid:
                auth_mode = getattr(settings, "AUTH_MODE", "cookies").lower()
                is_first_login = not self.auth.session_exists

                console.print("[bold red]Sessão do Moodle inativa ou expirada.[/bold red]")

                if auth_mode == "credentials":
                    console.print("[cyan]Tentando login automático por credenciais...[/cyan]")
                    auth_success = await self.auth.ensure_authenticated()
                    if auth_success:
                        self._session_expired_alerted = False
                        valid, user = await self.auth.validate_session()
                        if self.notifier.token and self.notifier.channel_id:
                            await self.notifier.send_session_renewed_notification(user_name=user)
                    else:
                        console.print("[red]Não foi possível autenticar automaticamente via credenciais.[/red]")
                        if not self._session_expired_alerted:
                            self._session_expired_alerted = True
                            if self.notifier.token and self.notifier.channel_id:
                                await self.notifier.send_session_expired_alert(browser_opened=False)
                        return
                else:
                    # Modo Cookies
                    if is_first_login:
                        console.print("[bold cyan]Primeiro login: Abrindo navegador no desktop para autenticação inicial...[/bold cyan]")
                        auth_success = await self.auth.interactive_login(headless=False)
                        if auth_success:
                            self._session_expired_alerted = False
                            valid, user = await self.auth.validate_session()
                            if self.notifier.token and self.notifier.channel_id:
                                await self.notifier.send_session_renewed_notification(user_name=user)
                        else:
                            console.print("[red]Primeiro login não foi concluído.[/red]")
                            return
                    else:
                        # Sessão expirou no modo cookies: NÃO abre navegador automaticamente!
                        console.print("[bold yellow]Modo cookies: Sessão expirada. O navegador não será aberto automaticamente.[/bold yellow]")
                        if not self._session_expired_alerted:
                            self._session_expired_alerted = True
                            if self.notifier.token and self.notifier.channel_id:
                                await self.notifier.send_session_expired_alert(browser_opened=False)
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

                # Sincronização automática com a Central de Estudos do Notion
                # REGRA CRÍTICA: Só sincroniza atividades que possuem DATA/PRAZO DEFINIDO.
                # Questionários e atividades sem prazo (ex: auto-estudo de Inglês Instrumental) NUNCA são adicionados ao Notion.
                if assign.is_actionable_pending:
                    if not assign.due_date:
                        if assign_state and not assign_state.get("notion_synced"):
                            assign_state["notion_synced"] = True
                            self.state.save()
                    else:
                        notion_synced = assign_state and assign_state.get("notion_synced", False)
                        if not notion_synced:
                            try:
                                from src.notifier.notion_client import notion_client
                                if notion_client.is_configured:
                                    date_iso = assign.due_date.strftime("%Y-%m-%d")
                                    r = await notion_client.create_task(
                                        title=assign.title,
                                        date_str=date_iso,
                                        category="TAREFA✅" if getattr(assign, "activity_type", "assign") != "quiz" else "TRABALHO🟡",
                                        course_name=assign.course_name,
                                        task_id_val=f"moodle_{assign.id}",
                                        notes_val=f"Atividade Moodle: {assign.title} ({assign.course_name})",
                                        details=f"Atividade detectada no Moodle UFMG com prazo em {assign.due_date_str or date_iso}.\nTipo: {'Questionário' if getattr(assign, 'activity_type', 'assign') == 'quiz' else 'Entrega de Arquivo'}",
                                        moodle_url=assign.url,
                                        steps=[
                                            f"Revisar anotações e conteúdos de {assign.course_name}",
                                            f"Resolver '{assign.title}'",
                                            "Validar e submeter no Moodle"
                                        ],
                                        notify_discord=True
                                    )
                                    if r.get("success"):
                                        self.state.register_assignment(assign)
                                        cur = self.state.get_assignment(assign.id)
                                        if cur:
                                            cur["notion_synced"] = True
                                            self.state.save()
                            except Exception as n_err:
                                console.print(f"[yellow]Aviso ao sincronizar tarefa {assign.title} no Notion: {n_err}[/yellow]")

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

                    # Gera o rascunho com a IA via fila centralizada
                    # auto_triggered=True → gera PDF (comportamento do daemon)
                    try:
                        from src.scheduler.queue_manager import queue_manager, QueueItem, QueueTaskType

                        async def _do_auto_solve(target_assign=assign):
                            d = await self.solver.solve_assignment(
                                target_assign,
                                auto_triggered=True,  # daemon/automático → PDF
                            )
                            self.state.register_assignment(target_assign, draft_path=str(d.output_path))
                            if self.notifier.token and self.notifier.channel_id:
                                await self.notifier.send_assignment_review(target_assign, d)
                            return True, f"Rascunho gerado para '{target_assign.title}'"

                        item = QueueItem(
                            task_type=QueueTaskType.RESOLVE_ASSIGNMENT,
                            title=assign.title,
                            course=assign.course_name,
                            requester="Daemon (Automático)",
                            coro_func=_do_auto_solve
                        )
                        await queue_manager.enqueue(item)
                    except Exception as sol_err:
                        console.print(f"[red]Erro ao enfileirar tarefa {assign.title}: {sol_err}[/red]")

                else:
                    self.state.register_assignment(assign)

            # 5. Atualização automática da checklist de tarefas do dia no Notion (uma vez ao dia)
            today_str = datetime.now().strftime("%Y-%m-%d")
            last_checklist_date = self.state.data.get("last_daily_checklist_date")
            if last_checklist_date != today_str:
                try:
                    from src.notifier.notion_client import notion_client
                    if notion_client.is_configured:
                        console.print(f"[cyan]Sincronizando tarefas do dia no Notion para hoje ({today_str})...[/cyan]")
                        chk_res = await notion_client.update_daily_checklist(notify_discord=True)
                        if chk_res.get("success"):
                            self.state.data["last_daily_checklist_date"] = today_str
                            self.state.save()
                except Exception as chk_err:
                    console.print(f"[yellow]Aviso ao atualizar checklist diária no Notion: {chk_err}[/yellow]")

            # Sincroniza estado com Render Hub (nuvem) se a ponte estiver configurada
            if settings.RENDER_URL:
                try:
                    from src.scheduler.bridge_runner import bridge_runner
                    await bridge_runner.publish_courses_to_hub()
                except Exception as b_err:
                    console.print(f"[yellow]Aviso ao sincronizar com Render Hub após varredura: {b_err}[/yellow]")

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

    async def _daily_checklist_job(self):
        """Dispara a atualização diária da checklist matinal no Notion às 07:00."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            from src.notifier.notion_client import notion_client
            if notion_client.is_configured:
                console.print("[cyan]Executando rotina matinal (07:00): atualizando tarefas do dia no Notion...[/cyan]")
                chk_res = await notion_client.update_daily_checklist(notify_discord=True)
                if chk_res.get("success"):
                    self.state.data["last_daily_checklist_date"] = today_str
                    self.state.save()
        except Exception as e:
            console.print(f"[yellow]Aviso no job diário da checklist do Notion: {e}[/yellow]")

    async def session_heartbeat_job(self):
        """Executa ping de keep-alive no Moodle para impedir expiração por inatividade e monitora status."""
        try:
            active, info = await self.auth.heartbeat_session()
            now_str = datetime.now().strftime("%H:%M:%S")
            if active:
                console.print(f"[dim cyan]💓 [Heartbeat {now_str}] Sessão do Moodle mantida ativa com sucesso![/dim cyan]")
                self._session_expired_alerted = False
            else:
                console.print(f"[bold yellow]⚠️ [Heartbeat {now_str}] Sessão do Moodle expirada ou inativa: {info}[/bold yellow]")
                auth_mode = getattr(settings, "AUTH_MODE", "cookies").lower()
                if not self._session_expired_alerted:
                    self._session_expired_alerted = True
                    if auth_mode == "credentials":
                        # No modo credenciais: auto-relogin em segundo plano
                        asyncio.create_task(self._auto_relogin_flow())
                    else:
                        # No modo cookies: NÃO abre navegador sozinho; notifica o Discord com o botão interativo
                        if self.notifier.token and self.notifier.channel_id:
                            await self.notifier.send_session_expired_alert(browser_opened=False)
        except Exception as e:
            console.print(f"[yellow]Nota no heartbeat da sessão: {e}[/yellow]")

    async def start(self):
        """Inicia o daemon e agenda os jobs em segundo plano."""
        self._running = True

        tray_status = "Inativa"
        if self.enable_tray and sys.platform == "win32":
            try:
                from src.ui.tray_manager import SystemTrayManager
                self.tray = SystemTrayManager(on_exit_callback=self.stop)
                if self.tray.start():
                    tray_status = "[bold green]Ativa (minimizar oculta da barra de tarefas)[/bold green]"
            except Exception as tray_err:
                tray_status = f"[yellow]Indisponível ({tray_err})[/yellow]"

        heartbeat_minutes = getattr(settings, "SESSION_HEARTBEAT_INTERVAL_MINUTES", 15) or 15
        console.print(
            Panel.fit(
                "[bold cyan]Moodle AI Assistant Daemon (UFMG)[/bold cyan]\n\n"
                f"• URL: [underline]{settings.MOODLE_BASE_URL}[/underline]\n"
                f"• Intervalo de varredura: [bold]{settings.CHECK_INTERVAL_MINUTES} minutos[/bold]\n"
                f"• Heartbeat Keep-Alive: [bold cyan]A cada {heartbeat_minutes} minutos[/bold cyan]\n"
                f"• IA: [bold]{settings.GEMINI_MODEL}[/bold]\n"
                f"• Bandeja do Sistema: {tray_status}\n"
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
            id="moodle_sync_job",
            misfire_grace_time=600,
            coalesce=True
        )

        # 3. Agenda o monitor de timeline / contagem regressiva (a cada 60s)
        self.scheduler.add_job(
            self.timeline_watcher,
            "interval",
            seconds=60,
            id="timeline_watcher_job",
            misfire_grace_time=300,
            coalesce=True
        )

        # 4. Agenda a atualização diária da checklist no Notion todos os dias às 07:00
        self.scheduler.add_job(
            self._daily_checklist_job,
            "cron",
            hour=7,
            minute=0,
            id="daily_checklist_notion_job",
            misfire_grace_time=3600,
            coalesce=True
        )

        # 5. Agenda o heartbeat silencioso de manutenção da sessão do Moodle
        self.scheduler.add_job(
            self.session_heartbeat_job,
            "interval",
            minutes=heartbeat_minutes,
            id="moodle_session_heartbeat_job",
            misfire_grace_time=300,
            coalesce=True
        )

        self.scheduler.start()

        # 5. Inicia o Runner da Ponte Nuvem (Render Hub <-> Desktop) se RENDER_URL estiver configurado
        if settings.RENDER_URL:
            from src.scheduler.bridge_runner import bridge_runner
            asyncio.create_task(bridge_runner.run_loop())

        while self._running:
            await asyncio.sleep(1)

    def stop(self):
        """Para o daemon com segurança."""
        self._running = False
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        if self.scheduler.running:
            self.scheduler.shutdown()
        console.print("[bold yellow]Daemon encerrado com segurança.[/bold yellow]")


async def main():
    parser = argparse.ArgumentParser(description="Moodle AI Assistant Background Daemon")
    parser.add_argument("--once", action="store_true", help="Executa apenas um ciclo e encerra")
    parser.add_argument("--no-tray", action="store_true", help="Desabilita o ícone da bandeja do sistema")
    args = parser.parse_args()

    daemon = MoodleDaemon(enable_tray=not args.no_tray and not args.once)


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
