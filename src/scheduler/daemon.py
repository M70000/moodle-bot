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
        self.enable_tray = enable_tray
        self.auth = MoodleAuth()
        self.scraper = MoodleScraper(auth=self.auth)
        self.solver = AISolver()  # Multi-provider BYOK: Gemini/Claude/DeepSeek
        self.notifier = MoodleDiscordNotifier()
        self.state = DaemonState()
        self.scheduler = AsyncIOScheduler()
        self._running = False
        self._session_expired_alerted = False
        self._is_reauthenticating = False
        self.tray = None

    @property
    def lms_provider(self) -> str:
        """Retorna o provedor educacional configurado ('moodle', 'canvas' ou 'multi')."""
        return getattr(settings, "LMS_PROVIDER", "moodle").strip().lower()

    @property
    def is_moodle_enabled(self) -> bool:
        """Indica se a plataforma Moodle está ativa neste ciclo."""
        return self.lms_provider in ("moodle", "multi")

    @property
    def is_canvas_enabled(self) -> bool:
        """Indica se a plataforma Canvas LMS está ativa neste ciclo."""
        return (
            self.lms_provider in ("canvas", "multi")
            and bool(getattr(settings, "CANVAS_API_TOKEN", "").strip())
        )


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

    async def _process_assignment(self, assign):
        """Processa uma atividade individual (Moodle ou Canvas): notas, Notion, rascunho de IA e revisão."""
        assign_state = self.state.get_assignment(assign.id)
        platform = getattr(assign, "platform", "moodle")
        plat_label = "Canvas" if platform == "canvas" else "Moodle"

        # 1. Monitor de Notas & Feedback: verifica se o professor lançou a avaliação
        if assign.has_grade and assign.grade_value:
            already_notified = assign_state and assign_state.get("grade_notified", False)
            if not already_notified:
                console.print(
                    f"[bold green]🎉 NOTA DETECTADA ({plat_label}):[/bold green] "
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
                else:
                    self.state.register_assignment(assign, grade_notified=True)

        # 2. Se a atividade não é acionável pendente (ex: já enviada ou prazo vencido), registra e segue
        if not assign.is_actionable_pending:
            self.state.register_assignment(assign)
            return

        # 3. Se o usuário cancelou essa tarefa pelo Discord, respeita e não processa
        if assign_state and assign_state.get("status") == "cancelled":
            console.print(f"[dim]Tarefa {assign.title} marcada como cancelada pelo usuário. Ignorando.[/dim]")
            return

        # 4. Sincronização automática com a Central de Estudos do Notion
        # Só sincroniza atividades que possuem DATA/PRAZO DEFINIDO.
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
                            plat_id_prefix = "canvas" if platform == "canvas" else "moodle"
                            r = await notion_client.create_task(
                                title=assign.title,
                                date_str=date_iso,
                                category="TAREFA✅" if getattr(assign, "activity_type", "assign") != "quiz" else "TRABALHO🟡",
                                course_name=assign.course_name,
                                task_id_val=f"{plat_id_prefix}_{assign.id}",
                                notes_val=f"Atividade {plat_label}: {assign.title} ({assign.course_name})",
                                details=f"Atividade detectada no {plat_label} com prazo em {assign.due_date_str or date_iso}.\nTipo: {'Questionário' if getattr(assign, 'activity_type', 'assign') == 'quiz' else 'Entrega de Arquivo'}",
                                moodle_url=assign.url,
                                steps=[
                                    f"Revisar anotações e conteúdos de {assign.course_name}",
                                    f"Resolver '{assign.title}'",
                                    f"Validar e submeter no {plat_label}"
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

        # 5. Se for questionário online (quiz), registra no catálogo para acompanhamento em /tarefas (resolução sob demanda)
        if getattr(assign, "activity_type", "assign") == "quiz":
            self.state.register_assignment(assign)
            return

        # 6. Se a tarefa foi adiada pelo usuário e o tempo ainda não passou, pula
        if self.state.is_postponed(assign.id):
            console.print(f"[dim]Tarefa {assign.title} adiada pelo usuário. Notificações temporariamente em pausa.[/dim]")
            return

        # 7. Se a tarefa é nova e ainda não possui rascunho gerado
        has_draft = assign_state and assign_state.get("draft_path")
        if not has_draft:
            console.print(
                f"[bold yellow]Nova tarefa pendente identificada ({plat_label}):[/bold yellow] "
                f"{assign.title} ({assign.course_name})"
            )

            # Gera o rascunho com a IA via fila centralizada
            try:
                from src.scheduler.queue_manager import queue_manager, QueueItem, QueueTaskType

                async def _do_auto_solve(target_assign=assign):
                    d = await self.solver.solve_assignment(
                        target_assign,
                        auto_triggered=True,
                    )
                    self.state.register_assignment(target_assign, draft_path=str(d.output_path))
                    if self.notifier.token and self.notifier.channel_id:
                        await self.notifier.send_assignment_review(target_assign, d)
                    return True, f"Rascunho gerado para '{target_assign.title}'"

                item = QueueItem(
                    task_type=QueueTaskType.RESOLVE_ASSIGNMENT,
                    title=assign.title,
                    course=assign.course_name,
                    requester=f"Daemon ({plat_label})",
                    coro_func=_do_auto_solve
                )
                await queue_manager.enqueue(item)
            except Exception as sol_err:
                console.print(f"[red]Erro ao enfileirar tarefa {assign.title}: {sol_err}[/red]")
        else:
            self.state.register_assignment(assign)

    async def _scan_canvas(self):
        """Executa varredura de disciplinas, tarefas e comunicados do Canvas LMS."""
        if not self.is_canvas_enabled:
            return

        try:
            console.rule("[bold magenta]Varredura do Canvas LMS (Instructure)[/bold magenta]")
            from src.providers.canvas import CanvasAdapter
            canvas_adapter = CanvasAdapter()
            c_courses, c_assignments, c_announcements = await canvas_adapter.scan_all(
                sync_materials=True,
                sync_announcements=True,
            )

            # Registra cursos do Canvas no catálogo de estado
            known_courses = self.state.data.get("courses", [])
            for c in c_courses:
                if c.name not in known_courses:
                    known_courses.append(c.name)
            self.state.data["courses"] = known_courses
            self.state.save()

            # Processa comunicados do Canvas
            for ann in c_announcements:
                if not self.state.is_announcement_seen(ann.id):
                    console.print(f"[bold yellow]📢 NOVO AVISO CANVAS:[/bold yellow] {ann.title} ({ann.course_name})")
                    from src.scraper.moodle_scraper import CourseAnnouncement
                    moodle_ann = CourseAnnouncement(
                        id=ann.id,
                        course_id=ann.course_id,
                        course_name=ann.course_name,
                        title=f"[Canvas] {ann.title}",
                        message=ann.message,
                        author=ann.author,
                        date=ann.posted_at.strftime("%d/%m/%Y") if ann.posted_at else "",
                        url=ann.url
                    )
                    await self.notifier.send_course_announcement(moodle_ann)
                    self.state.mark_announcement_seen(moodle_ann)

            # Processa tarefas do Canvas através do pipeline unificado
            for assign in c_assignments:
                from src.scraper.moodle_scraper import Assignment
                sub_st = "Enviado" if assign.is_submitted else "Não enviado"
                if assign.submission_status in ("Avaliado", "Enviado"):
                    sub_st = assign.submission_status

                m_assign = Assignment(
                    id=assign.id,
                    course_id=assign.course_id,
                    course_name=assign.course_name,
                    title=assign.title,
                    url=assign.url,
                    description=assign.description,
                    due_date=assign.due_date,
                    due_date_str=assign.due_date_str,
                    time_remaining=assign.time_remaining or "",
                    submission_status=sub_st,
                    grade_value=assign.grade_value,
                    activity_type=assign.activity_type
                )
                m_assign.platform = "canvas"
                await self._process_assignment(m_assign)

            console.print(f"[green]✔ Canvas sincronizado: {len(c_courses)} cursos, {len(c_assignments)} tarefas, {len(c_announcements)} avisos.[/green]")
        except Exception as c_err:
            console.print(f"[bold yellow]⚠️ Falha na varredura do Canvas LMS: {c_err}[/bold yellow]")

    async def _run_moodle_cycle(self):
        """Executa o ciclo de varredura exclusivo do Moodle (UFMG Virtual)."""
        console.rule("[bold cyan]Varredura do Moodle (UFMG Virtual)[/bold cyan]")
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

        # Executa a varredura das disciplinas, tarefas e comunicados do Moodle
        courses, assignments, announcements = await self.scraper.scan_all(
            sync_materials=True,
            sync_announcements=True,
            known_announcement_ids=known_ann_ids
        )

        # Registra cursos do Moodle no estado
        known_courses = self.state.data.get("courses", [])
        for c in courses:
            if c.name not in known_courses:
                known_courses.append(c.name)
        self.state.data["courses"] = known_courses
        self.state.save()

        # Processa comunicados da turma dos professores
        for ann in announcements:
            if not self.state.is_announcement_seen(ann.id):
                is_recent = any(m in ann.date.lower() for m in ["set", "out", "nov", "dez", "hoje", "ontem"]) if ann.date else True
                if first_ann_run and not is_recent:
                    self.state.mark_announcement_seen(ann)
                    continue

                console.print(f"[bold yellow]📢 NOVO AVISO DETECTADO:[/bold yellow] {ann.title} ({ann.course_name})")
                await self.notifier.send_course_announcement(ann)
                self.state.mark_announcement_seen(ann)

        # Processa cada atividade pelo pipeline comum
        for assign in assignments:
            assign.platform = "moodle"
            await self._process_assignment(assign)

        console.print(f"[green]✔ Moodle sincronizado: {len(courses)} cursos, {len(assignments)} tarefas, {len(announcements)} avisos.[/green]")

    async def run_cycle(self):
        """Executa um ciclo completo de verificação, resolução e notificação conforme provedores ativos."""
        try:
            if not self.is_moodle_enabled and not self.is_canvas_enabled:
                console.print(
                    f"[bold yellow]⚠️ Nenhum provedor de LMS ativo configurado (LMS_PROVIDER='{self.lms_provider}').[/bold yellow]\n"
                    f"Verifique seu .env ou execute 'configurar.bat' para conectar sua instituição."
                )
                return

            # 1. Executa ciclo do Moodle apenas se estiver ativado
            if self.is_moodle_enabled:
                await self._run_moodle_cycle()

            # 2. Executa varredura do Canvas LMS apenas se estiver ativado
            if self.is_canvas_enabled:
                await self._scan_canvas()

            # 3. Atualização automática da checklist de tarefas do dia no Notion (uma vez ao dia)
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

            # 4. Sincroniza estado com Render Hub (nuvem) se a ponte estiver configurada
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
        
        # Constrói o painel informativo baseado nos provedores educacionais ativos
        prov_info = []
        if self.is_moodle_enabled and self.is_canvas_enabled:
            panel_title = "[bold cyan]LumiBot Daemon • Modo Multi-LMS (Canvas + Moodle)[/bold cyan]"
            prov_info.append(f"• Provedor Ativo: [bold magenta]Multi-LMS (Híbrido)[/bold magenta]")
            prov_info.append(f"• Canvas URL: [underline]{settings.CANVAS_BASE_URL}[/underline]")
            prov_info.append(f"• Moodle URL: [underline]{settings.MOODLE_BASE_URL}[/underline]")
            prov_info.append(f"• Heartbeat Moodle: [dim cyan]A cada {heartbeat_minutes} minutos[/dim cyan]")
        elif self.is_canvas_enabled:
            panel_title = "[bold cyan]LumiBot Daemon • Canvas LMS[/bold cyan]"
            prov_info.append(f"• Provedor Ativo: [bold magenta]Canvas LMS (Instructure)[/bold magenta]")
            prov_info.append(f"• Canvas URL: [underline]{settings.CANVAS_BASE_URL}[/underline]")
            prov_info.append("• Autenticação: [bold green]Token de Acesso Pessoal (REST API)[/bold green]")
        elif self.is_moodle_enabled:
            panel_title = "[bold cyan]LumiBot Daemon • Moodle UFMG[/bold cyan]"
            prov_info.append(f"• Provedor Ativo: [bold cyan]Moodle (UFMG Virtual)[/bold cyan]")
            prov_info.append(f"• Moodle URL: [underline]{settings.MOODLE_BASE_URL}[/underline]")
            prov_info.append(f"• Heartbeat Moodle: [dim cyan]A cada {heartbeat_minutes} minutos[/dim cyan]")
        else:
            panel_title = "[bold yellow]LumiBot Daemon • Configuração Pendente[/bold yellow]"
            prov_info.append(f"• [bold red]Aviso: Nenhum provedor ativo detectado (LMS_PROVIDER='{self.lms_provider}')![/bold red]")
            prov_info.append("• Execute 'configurar.bat' para conectar sua instituição.")

        prov_lines_str = "\n".join(prov_info)
        console.print(
            Panel.fit(
                f"{panel_title}\n\n"
                f"{prov_lines_str}\n"
                f"• Intervalo de varredura: [bold]{settings.CHECK_INTERVAL_MINUTES} minutos[/bold]\n"
                f"• IA Principal: [bold]{settings.GEMINI_MODEL}[/bold]\n"
                f"• Bandeja do Sistema: {tray_status}\n"
                "• Submissão: [bold green]Estritamente sob aprovação humana[/bold green]\n"
                "• Alertas de prazo: [bold yellow]Contagem regressiva intensiva (15m, 5m, 2m, 1m)[/bold yellow]\n"
                "• Pressione [bold]Ctrl+C[/bold] para encerrar.",
                title="[bold green]LumiBot Ativo e Seguro[/bold green]",
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

        # 5. Agenda o heartbeat silencioso de manutenção da sessão do Moodle (apenas se Moodle ativo)
        if self.is_moodle_enabled:
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


_single_instance_mutex = None


def acquire_single_instance_lock() -> bool:
    """Garante que apenas uma instância do Daemon execute no Windows usando um Named Mutex."""
    global _single_instance_mutex
    if sys.platform != "win32":
        return True

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mutex_name = "Global\\MoodleAIAssistant_Daemon_SingleInstance_Mutex"
        _single_instance_mutex = kernel32.CreateMutexW(None, False, mutex_name)
        last_error = kernel32.GetLastError()
        ERROR_ALREADY_EXISTS = 183
        if last_error == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception as e:
        console.print(f"[yellow]Nota ao verificar trava de instância única: {e}[/yellow]")
        return True


async def main():
    parser = argparse.ArgumentParser(description="Moodle AI Assistant Background Daemon")
    parser.add_argument("--once", action="store_true", help="Executa apenas um ciclo e encerra")
    parser.add_argument("--no-tray", action="store_true", help="Desabilita o ícone da bandeja do sistema")
    args = parser.parse_args()

    # Trava de instância única para impedir que duas instâncias rodem simultaneamente
    if not args.once:
        if not acquire_single_instance_lock():
            console.print(
                Panel.fit(
                    "[bold yellow]⚠️ O Moodle AI Assistant já está em execução neste computador![/bold yellow]\n\n"
                    "Uma instância ativa foi detectada rodando em segundo plano (verifique a bandeja do sistema ao lado do relógio).\n"
                    "Esta janela adicional será encerrada para evitar duplicações e conflitos de sessão.",
                    title="[bold red]Instância Duplicada Detectada[/bold red]",
                    border_style="yellow"
                )
            )
            import time
            time.sleep(3)
            sys.exit(0)

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
