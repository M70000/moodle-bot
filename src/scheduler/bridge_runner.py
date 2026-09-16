"""Executor de tarefas despachadas pela Nuvem (Render Cloud Bridge Runner).

Executa em segundo plano no computador local do estudante, consumindo tarefas
aprovadas no Discord e executando as submissões reais no Moodle via Playwright.
"""

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from rich.console import Console

from config.settings import settings
from src.scraper.moodle_submitter import MoodleSubmitter

console = Console()


class BridgeRunner:
    """Consome a fila de submissão do Render e submete no Moodle localmente."""

    def __init__(self, render_url: str = ""):
        self.render_url = (render_url or settings.RENDER_URL or "").rstrip("/")
        self._is_running = False
        self._last_courses_publish: float = 0.0
        self._COURSES_PUBLISH_INTERVAL = 120.0  # Publica cursos a cada 2 minutos
        self._failed_materials = set()

    async def poll_once(self) -> int:
        """Consulta o Render e executa qualquer submissão pendente para este canal."""
        url_base = self.render_url or (settings.RENDER_URL or "").rstrip("/")
        if not url_base:
            return 0

        url = f"{url_base}/api/bridge/pending"

        loop = asyncio.get_running_loop()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MoodleDesktopRunner/1.0"})
            resp_bytes = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=8).read())
            data = json.loads(resp_bytes.decode("utf-8"))
            tasks = data.get("tasks", [])
        except Exception:
            return 0

        executed = 0
        for task in tasks:
            task_id = task.get("task_id")
            if not task_id:
                continue

            console.print(f"[bold green]📥 Tarefa recebida da Nuvem (ID: {task_id}):[/bold green] {task.get('title')} ({task.get('action')})")

            # Marca como in_progress no Render
            await self._claim_task(task_id, url_base)

            # Executa a ação localmente
            try:
                success, message = await self._execute_task(task)
            except Exception as exc:
                console.print(f"[bold red]❌ Erro inesperado ao executar tarefa {task_id}: {exc}[/bold red]")
                import traceback
                traceback.print_exc()
                success = False
                message = f"Erro inesperado no executor local: {exc}"

            # Reporta de volta ao Render para atualizar o Discord
            await self._report_complete(task_id, success, message, url_base)
            executed += 1

        # Publica lista de cursos e sincroniza materiais periodicamente (heartbeat de presença)
        import time
        now = time.time()
        if now - self._last_courses_publish > self._COURSES_PUBLISH_INTERVAL:
            await self.publish_courses_to_hub(url_base)
            await self.sync_custom_materials_from_hub(url_base)
            self._last_courses_publish = now

        return executed

    async def _claim_task(self, task_id: str, url_base: str):
        loop = asyncio.get_running_loop()
        try:
            url = f"{url_base}/api/bridge/claim"
            payload = json.dumps({"task_id": task_id}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5))
        except Exception:
            pass

    async def _report_complete(self, task_id: str, success: bool, message: str, url_base: str):
        loop = asyncio.get_running_loop()
        try:
            url = f"{url_base}/api/bridge/complete"
            payload = json.dumps({
                "task_id": task_id,
                "success": success,
                "message": message
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=8))
        except Exception as e:
            console.print(f"[red]Erro ao reportar conclusão da tarefa {task_id} ao Render: {e}[/red]")

    async def publish_courses_to_hub(self, url_base: str = "") -> bool:
        """Publica a lista de disciplinas, catálogo de tarefas e materiais no Render Hub (heartbeat de presença)."""
        base = url_base or self.render_url or (settings.RENDER_URL or "").rstrip("/")
        if not base:
            return False
        loop = asyncio.get_running_loop()
        try:
            from src.notifier.discord_bot import get_available_courses
            from src.scheduler.state import DaemonState
            courses = get_available_courses()
            try:
                state = DaemonState()
                assignments = state.data.get("assignments", {})
                raw_materials = state.get_custom_materials()
                custom_materials = [
                    m for m in raw_materials
                    if "magicmock" not in str(m).lower()
                ]
            except Exception:
                assignments = {}
                custom_materials = []

            url = f"{base}/api/bridge/sync"
            ch_id = str(getattr(settings, "DISCORD_CHANNEL_ID", "") or "")
            payload = json.dumps({
                "channel_id": ch_id,
                "courses": courses,
                "assignments": assignments,
                "custom_materials": custom_materials
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "MoodleDesktopRunner/1.0"}
            )
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=8))
            console.print(f"[cyan]🔗 [Ponte] {len(courses)} disciplinas, {len(assignments)} tarefas e {len(custom_materials)} materiais sincronizados com o Hub.[/cyan]")
            return True
        except Exception as e:
            console.print(f"[yellow]Aviso ao publicar estado no Hub: {e}[/yellow]")
            return False

    async def sync_custom_materials_from_hub(self, url_base: str = "") -> int:
        """Sincroniza e baixa materiais enviados via Discord na Nuvem (Render Hub) para a máquina local."""
        base = url_base or self.render_url or (settings.RENDER_URL or "").rstrip("/")
        if not base:
            return 0

        loop = asyncio.get_running_loop()
        try:
            ch_id = str(getattr(settings, "DISCORD_CHANNEL_ID", "") or "")
            query_str = f"?channel_id={urllib.parse.quote(ch_id)}" if ch_id else ""
            url = f"{base}/api/bridge/materials{query_str}"
            req = urllib.request.Request(url, headers={"User-Agent": "MoodleDesktopRunner/1.0"})
            resp_bytes = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10).read())
            data = json.loads(resp_bytes.decode("utf-8"))
            mats = data.get("materials", [])
        except Exception:
            return 0

        if not mats:
            return 0

        downloaded = 0
        from src.notifier.discord_bot import resolve_course_materials_dir, sanitize_filename
        from src.scheduler.state import DaemonState
        state = DaemonState()

        for item in mats:
            course = item.get("course")
            filename = item.get("filename")
            att_url = item.get("attachment_url")
            if not course or not filename or not att_url:
                continue

            # Ignora materiais com MagicMock ou que falharam anteriormente com 403/404
            if (
                "magicmock" in str(item).lower()
                or not att_url.startswith("http")
                or att_url in self._failed_materials
            ):
                continue

            target_dir = resolve_course_materials_dir(course)
            target_file = target_dir / sanitize_filename(filename)

            if target_file.exists() and target_file.stat().st_size > 0:
                state.register_custom_material(
                    course=target_dir.name,
                    filename=target_file.name,
                    attachment_url=att_url,
                    channel_id=item.get("channel_id"),
                    message_id=item.get("message_id"),
                    uploader=item.get("uploader", ""),
                    size=item.get("size", 0)
                )
                continue

            try:
                console.print(f"[cyan]Baixando material adicionado via Discord: [bold]{filename}[/bold] ({course})...[/cyan]")
                req_file = urllib.request.Request(
                    att_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
                )
                content = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req_file, timeout=30).read())
                if content:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(content)
                    state.register_custom_material(
                        course=target_dir.name,
                        filename=target_file.name,
                        attachment_url=att_url,
                        channel_id=item.get("channel_id"),
                        message_id=item.get("message_id"),
                        uploader=item.get("uploader", ""),
                        size=len(content)
                    )
                    downloaded += 1
                    console.print(f"[green]✔ Material sincronizado no PC local:[/green] {target_file.name} em {target_dir.name}")
            except urllib.error.HTTPError as http_err:
                if http_err.code in (403, 404):
                    self._failed_materials.add(att_url)
                console.print(f"[yellow]Aviso ao baixar material {filename} da ponte: {http_err}[/yellow]")
            except Exception as dl_err:
                console.print(f"[yellow]Aviso ao baixar material {filename} da ponte: {dl_err}[/yellow]")

        return downloaded

    async def sync_state_to_hub(self, url_base: str = "") -> bool:
        """Alias conveniente para sincronização forçada de estado."""
        res = await self.publish_courses_to_hub(url_base)
        await self.sync_custom_materials_from_hub(url_base)
        return res

    async def _execute_task(self, task: dict):
        action = task.get("action")
        assignment_url = task.get("assignment_url")
        title = task.get("title", "Atividade")

        console.print(f"[bold green]📥 [Ponte Nuvem] Executando submissão aprovada no Discord: {title} ({action})[/bold green]")
        submitter = MoodleSubmitter()

        is_canvas = (
            "instructure.com" in str(assignment_url or "")
            or task.get("platform") == "canvas"
            or str(task.get("assignment_id", "")).startswith(("canvas_", "c_"))
            or "/quizzes/" in str(assignment_url or "")
        )

        if action == "approve_assign":
            if is_canvas:
                from src.providers.canvas import CanvasSubmitter, extract_canvas_ids
                c_id, a_id = extract_canvas_ids(assignment_url or task.get("assignment_id", ""), task.get("course_id"))
                snap_url = task.get("snapshot_url") or task.get("url")
                if snap_url and str(snap_url).startswith("http"):
                    return await CanvasSubmitter().submit_url(course_id=c_id or "101", assignment_id=a_id or str(task.get("assignment_id")), url=str(snap_url))

                # Suporte a submissão de texto online (online_text_entry)
                text_to_submit = task.get("text_to_submit") or task.get("prepared_response") or task.get("body")
                sub_types = task.get("submission_types") or []
                if text_to_submit and ("online_text_entry" in sub_types or not task.get("file_to_submit")):
                    return await CanvasSubmitter().submit_text(
                        course_id=c_id or "101",
                        assignment_id=a_id or str(task.get("assignment_id")),
                        body=str(text_to_submit),
                        comment="Submetido via LumiBot",
                    )

            file_path_str = task.get("file_to_submit")
            if not file_path_str or not Path(file_path_str).exists():
                # Busca na pasta submissions se não tiver o caminho completo
                sub_dir = Path(settings.STORAGE_SUBMISSIONS_DIR)
                matches = list(sub_dir.glob(f"*{task.get('assignment_id')}*.pdf"))
                if not matches:
                    matches = list(sub_dir.glob(f"*{task.get('assignment_id')}*.docx"))
                if matches:
                    file_path = matches[0]
                else:
                    return False, f"Arquivo PDF/DOCX para a atividade {task.get('assignment_id')} não encontrado no disco local."
            else:
                file_path = Path(file_path_str)

            if is_canvas:
                from src.providers.canvas import CanvasSubmitter, extract_canvas_ids
                c_id, a_id = extract_canvas_ids(assignment_url or task.get("assignment_id", ""), task.get("course_id"))
                return await CanvasSubmitter().submit_assignment(course_id=c_id or "101", assignment_id=a_id or str(task.get("assignment_id")), file_path=file_path)
            else:
                return await submitter.submit_assignment(assignment_url=assignment_url, file_path=file_path)


        elif action == "fill_quiz":
            answers = task.get("structured_answers") or {}
            if is_canvas:
                from src.providers.canvas import CanvasSubmitter
                return await CanvasSubmitter().submit_quiz(quiz_url=assignment_url, answers=answers, auto_submit=False)
            else:
                return await submitter.submit_quiz(quiz_url=assignment_url, answers=answers, auto_submit=False)

        elif action == "finalize_quiz":
            answers = task.get("structured_answers") or {}
            if is_canvas:
                from src.providers.canvas import CanvasSubmitter
                return await CanvasSubmitter().submit_quiz(quiz_url=assignment_url, answers=answers, auto_submit=True)
            else:
                return await submitter.submit_quiz(quiz_url=assignment_url, answers=answers, auto_submit=True)


        elif action in ("solve_task", "redo_task"):
            import discord
            from src.notifier.discord_bot import _execute_solve_flow, bot, MoodleDiscordNotifier
            setattr(bot, "_skip_tree_sync", True)
            notifier = MoodleDiscordNotifier()
            target_ch_id = int(task.get("channel_id") or settings.DISCORD_CHANNEL_ID or 0)
            target_ch = None
            if target_ch_id:
                try:
                    target_ch = await notifier._resolve_channel(target_ch_id)
                except Exception as ch_err:
                    console.print(f"[yellow]Nota ao obter canal do Discord {target_ch_id}: {ch_err}[/yellow]")

            async def _send_via_discord(*args, **kwargs):
                try:
                    if target_ch and hasattr(target_ch, "send"):
                        return await target_ch.send(*args, **kwargs)
                except Exception as ch_send_err:
                    console.print(f"[yellow]Nota ao enviar no canal {target_ch_id}: {ch_send_err}[/yellow]")

                if settings.DISCORD_CHANNEL_ID:
                    try:
                        fallback_ch = await notifier._resolve_channel(settings.DISCORD_CHANNEL_ID)
                        if fallback_ch and hasattr(fallback_ch, "send"):
                            return await fallback_ch.send(*args, **kwargs)
                    except Exception:
                        pass
                return None

            answers = task.get("structured_answers") or {}
            instrucoes = answers.get("instrucoes")
            extra_files_str = answers.get("extra_files") or []
            extra_paths = []
            for p_str in extra_files_str:
                p = Path(p_str)
                if p.exists() and p.is_file():
                    extra_paths.append(p)
                    continue

                filename = p.name
                course_name = task.get("course", "")
                from src.scraper.moodle_scraper import sanitize_filename
                candidates = []
                if course_name:
                    candidates.append(Path(settings.STORAGE_MATERIALS_DIR) / sanitize_filename(course_name) / filename)
                    candidates.append(Path(settings.STORAGE_MATERIALS_DIR) / course_name / filename)

                mat_root = Path(settings.STORAGE_MATERIALS_DIR)
                if mat_root.exists():
                    for found_path in mat_root.rglob(filename):
                        if found_path.is_file():
                            candidates.append(found_path)
                            break

                candidates.append(Path("storage/submissions/temp_uploads") / filename)

                for cand in candidates:
                    if cand.exists() and cand.is_file():
                        extra_paths.append(cand)
                        break

            if extra_paths:
                console.print(f"[cyan]📚 Materiais de apoio identificados no desktop ({len(extra_paths)}):[/cyan] {', '.join(p.name for p in extra_paths)}")
            elif extra_files_str:
                console.print(f"[yellow]⚠️ Materiais solicitados pela nuvem não encontrados localmente: {extra_files_str}[/yellow]")

            modo = answers.get("modo", "resolver")
            raw_url = (task.get("assignment_url") or "").strip()
            is_valid_act_url = raw_url.startswith("http") and ("/mod/" in raw_url or "/courses/" in raw_url or "/assignments/" in raw_url or "/quizzes/" in raw_url)

            assign_id = str(task.get("assignment_id") or "").strip()
            is_real_id = assign_id and not assign_id.startswith("custom_")

            if is_valid_act_url:
                tarefa_target = raw_url
            elif is_real_id:
                tarefa_target = assign_id
            elif answers.get("tarefa") and not str(answers.get("tarefa")).startswith("http"):
                tarefa_target = answers.get("tarefa")
            else:
                tarefa_target = task.get("title") or answers.get("tarefa") or ""

            from src.scheduler.queue_manager import queue_manager, QueueItem, QueueTaskType

            is_quiz = (
                (is_valid_act_url and ("mod/quiz" in raw_url.lower() or "/quizzes/" in raw_url.lower()))
                or task.get("activity_type") == "quiz"
                or "quiz" in title.lower()
                or "aula" in title.lower()
            )
            if action == "redo_task":
                task_type = QueueTaskType.REDO_TASK
            elif modo == "finalizar":
                task_type = QueueTaskType.PIPELINE_COMPLETE
            elif modo == "preencher":
                task_type = QueueTaskType.PIPELINE_FILL
            elif is_quiz:
                task_type = QueueTaskType.RESOLVE_QUIZ
            else:
                task_type = QueueTaskType.RESOLVE_ASSIGNMENT

            done_event = asyncio.Event()
            result_box = {"success": False, "message": ""}

            async def _bridge_solve_coro():
                res = await _execute_solve_flow(
                    send_func=_send_via_discord,
                    tarefa=tarefa_target,
                    instrucoes=instrucoes,
                    extra_files=extra_paths,
                    is_refazer=(action == "redo_task"),
                    modo=modo,
                    channel=target_ch,
                    expected_course=task.get("course", "")
                )
                if isinstance(res, tuple) and len(res) == 2:
                    return res
                return True, f"Resolução de '{title}' concluída com sucesso pelo Desktop Runner."

            async def _on_bridge_finish(success: bool, msg: str):
                result_box["success"] = success
                result_box["message"] = msg
                done_event.set()

            q_item = QueueItem(
                task_type=task_type,
                title=title,
                course=task.get("course", "Geral"),
                requester=task.get("requester", "Nuvem (Discord)"),
                coro_func=_bridge_solve_coro,
                on_finish=_on_bridge_finish
            )

            await queue_manager.enqueue(q_item)
            await done_event.wait()
            return result_box["success"], result_box["message"]

        elif action == "study_question":
            import discord
            from src.solver.study_tutor import StudyTutor
            from src.notifier.discord_bot import clean_display_course, MoodleDiscordNotifier
            answers = task.get("structured_answers") or {}
            disciplina = answers.get("disciplina") or task.get("course", "")
            duvida = answers.get("duvida", "")
            material = answers.get("material")
            user_mention = answers.get("user_mention", "")

            notifier = MoodleDiscordNotifier()
            target_ch_id = int(task.get("channel_id") or settings.DISCORD_CHANNEL_ID or 0)
            target_ch = await notifier._resolve_channel(target_ch_id) if target_ch_id else None

            from src.ui.theme import LumiTheme, apply_lumi_footer
            tutor = StudyTutor()
            res = await tutor.answer_question(discipline=disciplina, question=duvida, specific_material=material)

            disc_clean = clean_display_course(disciplina)
            embed = discord.Embed(
                title=f"💡 Tutor Acadêmico: {disc_clean}",
                color=LumiTheme.PRIMARY
            )
            embed.add_field(name="❓ Dúvida do Aluno", value=f"*{duvida[:500]}*", inline=False)

            ans_text = res.get("answer", "")
            if len(ans_text) <= 4000:
                embed.description = f"### 📖 Resposta do Tutor\n\n{ans_text}"
            else:
                embed.description = f"### 📖 Resposta do Tutor\n\n{ans_text[:3900]}\n\n*(continua no próximo campo...)*"
                embed.add_field(name="📖 Continuação", value=ans_text[3900:4900], inline=False)

            mats = res.get("materials_used", [])
            if mats:
                embed.add_field(name="📚 Materiais & Slides Consultados", value="\n".join(f"• `{m}`" for m in mats[:4]), inline=False)

            requester = task.get("requester") or "Estudante"
            apply_lumi_footer(embed, extra_info=f"Solicitado por {requester} • Modelo: {res.get('model_used')}")

            msg_content = f"{user_mention} aqui está a resposta para a sua dúvida:" if user_mention else ""
            if target_ch and hasattr(target_ch, "send"):
                await target_ch.send(content=msg_content, embed=embed)
            return True, f"Dúvida sobre '{disciplina}' respondida com sucesso pelo Desktop Runner."

        elif action == "study_flashcards":
            import discord
            from src.solver.study_tutor import StudyTutor
            from src.notifier.discord_bot import FlashcardsCarouselView, MoodleDiscordNotifier
            answers = task.get("structured_answers") or {}
            disciplina = answers.get("disciplina") or task.get("course", "")
            topico = answers.get("topico")
            qtd = int(answers.get("qtd") or 8)
            material = answers.get("material")
            user_mention = answers.get("user_mention", "")
            requester = task.get("requester") or "Estudante"

            notifier = MoodleDiscordNotifier()
            target_ch_id = int(task.get("channel_id") or settings.DISCORD_CHANNEL_ID or 0)
            target_ch = await notifier._resolve_channel(target_ch_id) if target_ch_id else None

            tutor = StudyTutor()
            res = await tutor.generate_flashcards(discipline=disciplina, topic=topico, count=qtd, specific_material=material)

            view = FlashcardsCarouselView(
                cards=res.get("cards", []),
                discipline=disciplina,
                topic=res.get("topic", "Geral"),
                requester=requester
            )
            embed = view.build_embed()

            anki_path = res.get("anki_file_path")
            file_to_send = None
            if anki_path and Path(anki_path).exists():
                file_to_send = discord.File(str(anki_path), filename=Path(anki_path).name)

            msg_content = f"{user_mention} aqui está o seu baralho de flashcards interativo e o arquivo Anki!" if user_mention else ""
            if target_ch and hasattr(target_ch, "send"):
                if file_to_send:
                    await target_ch.send(content=msg_content, embed=embed, view=view, file=file_to_send)
                else:
                    await target_ch.send(content=msg_content, embed=embed, view=view)
            return True, f"Baralho de flashcards para '{disciplina}' gerado com sucesso pelo Desktop Runner."

        elif action == "study_quiz":
            import discord
            from src.solver.study_tutor import StudyTutor
            from src.notifier.discord_bot import InteractiveQuizSessionView, MoodleDiscordNotifier
            answers = task.get("structured_answers") or {}
            disciplina = answers.get("disciplina") or task.get("course", "")
            topico = answers.get("topico")
            qtd = int(answers.get("qtd") or 5)
            material = answers.get("material")
            user_mention = answers.get("user_mention", "")
            requester = task.get("requester") or "Estudante"
            user_id = int(answers.get("user_id") or 0)

            notifier = MoodleDiscordNotifier()
            target_ch_id = int(task.get("channel_id") or settings.DISCORD_CHANNEL_ID or 0)
            target_ch = await notifier._resolve_channel(target_ch_id) if target_ch_id else None

            tutor = StudyTutor()
            res = await tutor.generate_quiz(discipline=disciplina, num_questions=qtd, topic=topico, specific_material=material)

            view = InteractiveQuizSessionView(
                questions=res.get("questions", []),
                discipline=disciplina,
                topic=res.get("topic", "Geral"),
                requester=requester,
                requester_id=user_id
            )
            embed = view.build_question_embed()

            msg_content = f"{user_mention} seu simulado interativo começou! Responda nos botões abaixo:" if user_mention else ""
            if target_ch and hasattr(target_ch, "send"):
                await target_ch.send(content=msg_content, embed=embed, view=view)
            return True, f"Simulado de '{disciplina}' gerado com sucesso pelo Desktop Runner."

        elif action == "notion_checklist":
            from src.notifier.notion_client import notion_client
            if not notion_client.is_configured:
                return False, "Integração com Notion não está configurada no Desktop Runner (.env local)."
            res = await notion_client.update_daily_checklist(notify_discord=True)
            if res.get("success"):
                return True, f"Checklist do dia atualizada no Notion ({res.get('tasks_count')} tarefas)."
            return False, f"Erro ao atualizar checklist no Notion: {res.get('error')}"

        elif action == "notion_sync":
            from src.notifier.notion_client import notion_client
            from src.scheduler.state import DaemonState
            if not notion_client.is_configured:
                return False, "Integração com Notion não está configurada no Desktop Runner (.env local)."
            state = DaemonState()
            assignments = state.get_pending_assignments()
            if not assignments:
                return True, "Nenhuma tarefa pendente encontrada no catálogo local para sincronizar com o Notion."

            added = 0
            already = 0
            for a in assignments:
                if not a.due_date:
                    continue
                tid = f"moodle_{a.id}"
                date_iso = a.due_date.strftime("%Y-%m-%d")
                r = await notion_client.create_task(
                    title=a.title,
                    date_str=date_iso,
                    category="TAREFA✅" if getattr(a, "activity_type", "assign") != "quiz" else "TRABALHO🟡",
                    course_name=a.course_name,
                    task_id_val=tid,
                    notes_val=f"Atividade Moodle: {a.title} ({a.course_name})",
                    details=f"Atividade do Moodle com vencimento em {a.due_date_str or 'Data não informada'}.\nStatus no Moodle: {a.status_text or 'Pendente'}",
                    moodle_url=a.url,
                    steps=[
                        f"Revisar conceitos e anotações de {a.course_name}",
                        f"Resolver '{a.title}'",
                        "Conferir envio no Moodle"
                    ],
                    notify_discord=True
                )
                if r.get("success"):
                    if r.get("already_exists"):
                        already += 1
                    else:
                        added += 1

            if added == 0 and already == 0:
                return True, "Nenhuma tarefa pendente com prazo definido foi encontrada para sincronizar com o Notion."

            return True, f"Sincronização com Notion concluída: {added} adicionados, {already} já existentes."

        elif action == "relogin":
            from src.auth.moodle_auth import MoodleAuth
            auth = MoodleAuth()
            console.print("[bold cyan]🔑 [Ponte Nuvem] Comando de login recebido: Abrindo navegador no desktop...[/bold cyan]")
            success = await auth.interactive_login(headless=False)
            if success:
                valid, user = await auth.validate_session()
                from src.notifier.discord_bot import MoodleDiscordNotifier
                notifier = MoodleDiscordNotifier()
                await notifier.send_session_renewed_notification(user_name=user)
                return True, "Sessão renovada com sucesso via navegador interativo."
            return False, "Navegador de login foi fechado sem autenticação concluída."

        return False, f"Ação desconhecida: {action}"

    async def run_loop(self, poll_interval: float = 4.0):
        """Loop contínuo de polling da ponte."""
        self._is_running = True
        url_base = self.render_url or (settings.RENDER_URL or "").rstrip("/")
        console.print(f"[cyan]🔗 Ponte Nuvem (Render Hub) ativa para submissões remotas: {url_base}[/cyan]")

        # Publica disciplinas imediatamente ao iniciar (heartbeat inicial)
        if url_base:
            await self.publish_courses_to_hub(url_base)
        import time
        self._last_courses_publish = time.time()

        while self._is_running:
            try:
                await self.poll_once()
            except Exception:
                pass

            # Heartbeat periódico a cada 60s para manter status online e catálogo atualizado
            if url_base and (time.time() - getattr(self, "_last_courses_publish", 0)) > 60:
                try:
                    await self.publish_courses_to_hub(url_base)
                    self._last_courses_publish = time.time()
                except Exception:
                    pass

            await asyncio.sleep(poll_interval)


bridge_runner = BridgeRunner()
