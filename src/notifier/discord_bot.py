"""Módulo de Notificações, Revisão e Slash Commands via Discord Bot.

Inclui comandos interativos:
  /tarefas              - Lista tarefas pendentes e entregues com prazos
  /materiais            - Envia slides e materiais de estudo de uma disciplina
  /resolver             - Resolve ou refaz tarefas sob demanda com instruções e anexos
  /status               - Exibe o estado da sessão do Moodle, materiais e IA
  /adicionarconteudo    - Envia novos materiais e resumos salvando na memória da IA
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
import re
import unicodedata

import discord
from discord import app_commands, ui
from discord.ext import commands
from rich.console import Console

from config.settings import settings
from src.auth.moodle_auth import MoodleAuth
from src.scheduler.state import DaemonState
from src.scraper.moodle_scraper import Assignment, CourseMaterial, sanitize_filename
from src.scraper.moodle_submitter import MoodleSubmitter
from src.solver.gemini_solver import GeminiSolver, SolutionDraft

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


def normalize_text(text: Optional[Any]) -> str:
    """Remove acentos e padroniza para buscas tolerantes, imune a valores None."""
    if text is None:
        return ""
    return unicodedata.normalize("NFKD", str(text)).encode("ASCII", "ignore").decode("utf-8").lower()


def clean_display_course(course: str) -> str:
    """Simplifica nomes longos de disciplina para caber elegantemente nas opções do Discord."""
    c = re.sub(r"^\d{4}_\d\s*-\s*", "", course)
    c = re.sub(r"\s*-\s*(T[A-Z0-9]+|METATURMA)$", "", c, flags=re.IGNORECASE).strip()
    return c.title() if c else course


def get_available_courses() -> List[str]:
    """Retorna os nomes das disciplinas que possuem pasta em storage/materials/."""
    mat_dir = settings.STORAGE_MATERIALS_DIR
    if not mat_dir.exists():
        return []
    return [p.name for p in mat_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]


async def course_autocomplete(
    interaction: discord.Interaction,
    current: Optional[str] = ""
) -> List[app_commands.Choice[str]]:
    """Autocomplete interativo para seleção de disciplinas no Discord (tolerante a acentos)."""
    try:
        courses = get_available_courses()
        norm_curr = normalize_text(current)
        filtered = [c for c in courses if not norm_curr or norm_curr in normalize_text(c)]
        return [
            app_commands.Choice(name=c[:100], value=c)
            for c in filtered[:25]
        ]
    except Exception as err:
        console.print(f"[bold red]Aviso no course_autocomplete: {err}[/bold red]")
        return []


def _is_task_completed(item: Dict[str, Any]) -> bool:
    """Verifica se uma tarefa no catálogo está com status de concluída/submetida."""
    if item.get("is_submitted"):
        return True
    if item.get("status") in ["submitted", "cancelled"]:
        return True
    sub_status = str(item.get("submission_status", "")).lower()
    if any(t in sub_status for t in ["concluído", "concluido", "enviado para avaliação", "feito", "finalizada"]):
        return True
    time_rem = str(item.get("time_remaining", "")).lower()
    if "enviada" in time_rem and "adiantado" in time_rem:
        return True
    return False


async def pending_task_autocomplete(
    interaction: discord.Interaction,
    current: Optional[str] = ""
) -> List[app_commands.Choice[str]]:
    """Autocomplete para /resolver: exibe exclusivamente tarefas e questionários pendentes."""
    try:
        state = DaemonState()
        assignments = state.data.get("assignments", {})
        norm_curr = normalize_text(current)
        candidates = []
        for aid, item in assignments.items():
            if _is_task_completed(item):
                continue  # Oculta tarefas já concluídas para evitar poluição no menu de resolução

            title = str(item.get("title") or aid).strip()
            course_raw = str(item.get("course") or "").strip()
            course_clean = clean_display_course(course_raw)
            due_str = str(item.get("due_date") or "").strip()
            due = f" (Prazo: {due_str})" if due_str else ""

            label = f"{title} - {course_clean}{due}" if course_clean else f"{title}{due}"
            label = label[:100].strip() or f"Tarefa {aid}"

            search_target = f"{normalize_text(title)} {normalize_text(course_raw)} {normalize_text(course_clean)} {normalize_text(aid)}"
            if not norm_curr or norm_curr in search_target:
                has_deadline = 1 if due_str else 0
                candidates.append((has_deadline, label, str(aid)))

        # Prioriza no menu as atividades que possuem prazo definido
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

        choices = [
            app_commands.Choice(name=label, value=aid)
            for _, label, aid in candidates[:25]
        ]
        return choices
    except Exception as err:
        console.print(f"[bold red]Aviso no pending_task_autocomplete: {err}[/bold red]")
        return []


async def completed_task_autocomplete(
    interaction: discord.Interaction,
    current: Optional[str] = ""
) -> List[app_commands.Choice[str]]:
    """Autocomplete para /refazer: exibe exclusivamente tarefas e questionários já concluídos."""
    try:
        state = DaemonState()
        assignments = state.data.get("assignments", {})
        norm_curr = normalize_text(current)
        candidates = []
        for aid, item in assignments.items():
            if not _is_task_completed(item):
                continue  # Exibe apenas itens já concluídos/entregues para serem refeitos

            title = str(item.get("title") or aid).strip()
            course_raw = str(item.get("course") or "").strip()
            course_clean = clean_display_course(course_raw)

            label = f"{title} [Concluído] - {course_clean}" if course_clean else f"{title} [Concluído]"
            label = label[:100].strip() or f"Tarefa {aid}"

            search_target = f"{normalize_text(title)} {normalize_text(course_raw)} {normalize_text(course_clean)} {normalize_text(aid)}"
            if not norm_curr or norm_curr in search_target:
                candidates.append((label, str(aid)))

        choices = [
            app_commands.Choice(name=label, value=aid)
            for label, aid in candidates[:25]
        ]
        return choices
    except Exception as err:
        console.print(f"[bold red]Aviso no completed_task_autocomplete: {err}[/bold red]")
        return []


class ReviewActionView(ui.View):
    """Componente interativo com botões dinâmicos de decisão e persistência imediata em disco."""

    def __init__(
        self,
        assignment_id: str,
        assignment_url: str,
        file_to_submit: Optional[Path] = None,
        activity_type: str = "assign",
        structured_answers: Optional[List[Dict[str, Any]]] = None,
        on_action: Optional[Callable[[str, str, discord.Interaction], asyncio.Future]] = None,
        timeout: Optional[float] = None
    ):
        super().__init__(timeout=timeout)
        self.assignment_id = assignment_id
        self.assignment_url = assignment_url
        self.file_to_submit = file_to_submit
        self.activity_type = activity_type
        self.structured_answers = structured_answers or []
        self.on_action = on_action
        self._is_draft_saved = False

        self._build_buttons()

    def _build_buttons(self, draft_saved: bool = False):
        self.clear_items()

        if self.activity_type == "quiz":
            # 1. Apenas Preencher (Salva rascunho na tentativa sem enviar definitivamente)
            btn_fill = ui.Button(
                label="Apenas Preencher Quiz" if not draft_saved else "✔ Respostas Preenchidas",
                style=discord.ButtonStyle.primary if not draft_saved else discord.ButtonStyle.secondary,
                emoji="📝",
                custom_id=f"btn_fill_{self.assignment_id}",
                disabled=draft_saved
            )
            btn_fill.callback = self.fill_quiz_button
            self.add_item(btn_fill)

            # 2. Enviar tudo e terminar
            btn_finalize = ui.Button(
                label="Enviar Tudo e Terminar",
                style=discord.ButtonStyle.success,
                emoji="🚀",
                custom_id=f"btn_finalize_{self.assignment_id}"
            )
            btn_finalize.callback = self.finalize_quiz_button
            self.add_item(btn_finalize)
        else:
            # 1. Aprovar e Enviar PDF
            btn_approve = ui.Button(
                label="Aprovar e Enviar PDF",
                style=discord.ButtonStyle.success,
                emoji="📄",
                custom_id=f"btn_approve_{self.assignment_id}"
            )
            btn_approve.callback = self.approve_assign_button
            self.add_item(btn_approve)

        # 3. Adiar (+1h)
        btn_postpone = ui.Button(
            label="Adiar (+1h)",
            style=discord.ButtonStyle.secondary,
            emoji="⏱️",
            custom_id=f"btn_postpone_{self.assignment_id}"
        )
        btn_postpone.callback = self.postpone_button
        self.add_item(btn_postpone)

        # 4. Cancelar / Descartar
        btn_cancel = ui.Button(
            label="Cancelar / Descartar",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"btn_cancel_{self.assignment_id}"
        )
        btn_cancel.callback = self.cancel_button
        self.add_item(btn_cancel)

        # 5. Link direto para a atividade no Moodle (acesso sem atrito)
        if self.assignment_url:
            btn_moodle = ui.Button(
                label="Abrir no Moodle",
                style=discord.ButtonStyle.link,
                url=self.assignment_url,
                emoji="🔗"
            )
            self.add_item(btn_moodle)

    def _extract_answers_payload(self) -> Dict[str, Any]:
        ans_payload = {}
        if isinstance(self.structured_answers, list):
            for item in self.structured_answers:
                if isinstance(item, dict) and "key" in item:
                    ans_payload[item["key"]] = item["value"]
                elif isinstance(item, dict) and "field" in item:
                    ans_payload[item["field"]] = item.get("value", "")
        elif isinstance(self.structured_answers, dict):
            ans_payload = dict(self.structured_answers)

        # Fallback robusto: se não houver chaves estruturadas, recupera do arquivo de rascunho gerado
        if not ans_payload and self.file_to_submit:
            target_md = None
            if self.file_to_submit.suffix == ".md" and self.file_to_submit.exists():
                target_md = self.file_to_submit
            elif self.file_to_submit.parent.exists():
                same_dir_rascunhos = list(self.file_to_submit.parent.glob(f"{self.file_to_submit.stem}*_rascunho.md"))
                if same_dir_rascunhos:
                    target_md = same_dir_rascunhos[0]
                else:
                    any_rascunho = list(self.file_to_submit.parent.glob("*_rascunho.md"))
                    if any_rascunho:
                        target_md = any_rascunho[0]

            if target_md and target_md.exists():
                try:
                    text = target_md.read_text(encoding="utf-8")
                    q_matches = list(re.finditer(r"###\s*(?:Quest[ãa]o|Q)\s*(\d+)\s*\n+(.*?)(?=\n###|\Z)", text, re.DOTALL | re.IGNORECASE))
                    for m in q_matches:
                        q_num = m.group(1)
                        q_body = m.group(2).strip()
                        resp_m = re.search(r"\*\*(?:Resposta|Alternativa):\*\*\s*(.+)", q_body, re.IGNORECASE)
                        if resp_m:
                            ans_payload[f"Q{q_num}"] = resp_m.group(1).strip()
                        else:
                            items = re.findall(r"^\s*\d+\.\s*\*{0,2}(.*?)\*{0,2}\s*$", q_body, re.MULTILINE)
                            if items:
                                for idx_sub, sub_val in enumerate(items, 1):
                                    clean_val = sub_val.strip("* ").strip()
                                    if clean_val:
                                        ans_payload[f"Q{q_num}_{idx_sub}"] = clean_val
                except Exception:
                    pass

        return ans_payload

    async def fill_quiz_button(self, interaction: discord.Interaction):
        """Apenas preenche os campos do questionário e salva como rascunho (sem finalizar)."""
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.gold()
            embed.set_footer(
                text=f"Status: ⏳ Inserindo respostas no Moodle (Modo Rascunho) por {interaction.user.name}..."
            )

        await interaction.response.edit_message(embed=embed, view=self)

        console.print(
            f"[bold cyan]Preenchimento de rascunho solicitado no Discord para {self.assignment_id}![/bold cyan] "
            f"Preenchendo campos no Moodle sem submeter..."
        )
        await interaction.followup.send(
            content="⏳ **Preenchendo questionário no Moodle com cadência humana...** As respostas serão digitadas e salvas na tentativa sem submeter.",
            ephemeral=False
        )

        ans_payload = self._extract_answers_payload()
        submitter = MoodleSubmitter()
        current_time = datetime.now().strftime("%H:%M:%S")

        success, message = await submitter.submit_quiz(
            quiz_url=self.assignment_url,
            answers=ans_payload or self.structured_answers,
            auto_submit=False
        )

        if success:
            self._is_draft_saved = True
            self._build_buttons(draft_saved=True)

            if embed:
                embed.color = discord.Color.blue()
                embed.title = f"📝 Rascunho Salvo: {embed.title.replace('📋 Revisão: ', '').replace('📋 Revisão de Atividade: ', '')}"
                embed.set_footer(
                    text=f"Respostas salvas no Moodle às {current_time}. Aguardando sua conferência manual ou envio definitivo."
                )

            await interaction.message.edit(embed=embed, view=self)
            moodle_link_md = f"👉 **[Clique aqui para abrir sua tentativa no Moodle]({self.assignment_url})**\n\n" if self.assignment_url else ""
            await interaction.followup.send(
                content=(
                    f"🎉 **Respostas salvas no Moodle com sucesso!**\n"
                    f"{message}\n\n"
                    f"{moodle_link_md}"
                    f"• Quando terminar de conferir, você mesmo pode clicar em **'Enviar tudo e terminar'** diretamente no Moodle;\n"
                    f"• Ou, se preferir, pode clicar no botão **[🚀 Enviar Tudo e Terminar]** acima para o robô finalizar!"
                ),
                ephemeral=False
            )
        else:
            self._build_buttons(draft_saved=False)
            if embed:
                embed.color = discord.Color.red()
                embed.set_footer(
                    text=f"Falha ao preencher às {current_time}: {message[:100]}"
                )
            await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                content=f"⚠️ **Falha ao preencher questionário no Moodle:** {message}",
                ephemeral=False
            )

    async def finalize_quiz_button(self, interaction: discord.Interaction):
        """Finaliza e submete em definitivo o questionário no Moodle ('Enviar tudo e terminar')."""
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.gold()
            embed.set_footer(
                text=f"Status: 🔄 Finalizando e enviando tudo no Moodle por {interaction.user.name}..."
            )

        await interaction.response.edit_message(embed=embed, view=self)

        console.print(
            f"[bold cyan]Envio definitivo do questionário {self.assignment_id} solicitado no Discord![/bold cyan]"
        )
        await interaction.followup.send(
            content="🚀 **Finalizando questionário no Moodle...** Confirmando 'Enviar tudo e terminar'.",
            ephemeral=False
        )

        submitter = MoodleSubmitter()
        current_time = datetime.now().strftime("%H:%M:%S")

        ans_payload = self._extract_answers_payload()
        if self._is_draft_saved:
            success, message = await submitter.finalize_quiz(self.assignment_url)
            if not success and "não foi encontrado" in message.lower():
                # Fallback: tenta preencher e enviar em um passo só
                success, message = await submitter.submit_quiz(
                    quiz_url=self.assignment_url,
                    answers=ans_payload or self.structured_answers,
                    auto_submit=True
                )
        else:
            success, message = await submitter.submit_quiz(
                quiz_url=self.assignment_url,
                answers=ans_payload or self.structured_answers,
                auto_submit=True
            )

        if success:
            state = DaemonState()
            state.mark_submitted(self.assignment_id)

            if embed:
                embed.color = discord.Color.green()
                embed.title = f"✅ Submetido com Sucesso: {embed.title.replace('📋 Revisão: ', '').replace('📋 Revisão de Atividade: ', '').replace('📝 Rascunho Salvo: ', '')}"
                embed.set_footer(
                    text=f"Finalizado no Moodle às {current_time} por {interaction.user.name}"
                )
            await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                content=f"🎉 **Confirmação de Envio no Moodle:** {message}",
                ephemeral=False
            )
            if self.on_action:
                await self.on_action(self.assignment_id, "approved", interaction)
        else:
            self._build_buttons(draft_saved=self._is_draft_saved)
            if embed:
                embed.color = discord.Color.red()
                embed.set_footer(
                    text=f"Falha na finalização às {current_time}: {message[:100]}"
                )
            await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                content=f"⚠️ **Falha ao finalizar questionário:** {message}",
                ephemeral=False
            )

    async def approve_assign_button(self, interaction: discord.Interaction):
        """Aprova e submete tarefas de entrega de arquivo (PDF/Docx)."""
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.gold()
            embed.set_footer(
                text=f"Status: 🔄 Aprovado por {interaction.user.name}! Enviando arquivo no Moodle..."
            )

        await interaction.response.edit_message(embed=embed, view=self)

        submitter = MoodleSubmitter()
        current_time = datetime.now().strftime("%H:%M:%S")

        if not self.file_to_submit:
            await interaction.followup.send("⚠️ Nenhum arquivo foi anexado a este pedido.", ephemeral=True)
            return

        console.print(
            f"[bold cyan]Aprovação recebida no Discord para tarefa {self.assignment_id}![/bold cyan] "
            f"Disparando envio de {self.file_to_submit.name}..."
        )
        await interaction.followup.send(
            content=f"⏳ **Enviando arquivo no Moodle:** `{self.file_to_submit.name}`...",
            ephemeral=False
        )

        success, message = await submitter.submit_assignment(
            assignment_url=self.assignment_url,
            file_path=self.file_to_submit
        )

        if success:
            state = DaemonState()
            state.mark_submitted(self.assignment_id)

            if embed:
                embed.color = discord.Color.green()
                embed.title = f"✅ Submetido com Sucesso: {embed.title.replace('📋 Revisão: ', '').replace('📋 Revisão de Atividade: ', '')}"
                embed.set_footer(
                    text=f"Finalizado no Moodle às {current_time} por {interaction.user.name}"
                )
            await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                content=f"🎉 **Confirmação de Envio no Moodle:** {message}",
                ephemeral=False
            )
            if self.on_action:
                await self.on_action(self.assignment_id, "approved", interaction)
        else:
            self._build_buttons(draft_saved=False)
            if embed:
                embed.color = discord.Color.red()
                embed.set_footer(
                    text=f"Falha no envio às {current_time}: {message[:100]}"
                )
            await interaction.message.edit(embed=embed, view=self)
            await interaction.followup.send(
                content=f"⚠️ **Falha no envio:** {message}",
                ephemeral=False
            )

    async def postpone_button(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True

        state = DaemonState()
        until_str = state.postpone_assignment(self.assignment_id, hours=1)

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text=f"Status: ⏱️ Adiado por {interaction.user.name} até às {until_str}!")

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            content=f"⏱️ **Lembrete Adiado:** Notificações da tarefa `{self.assignment_id}` pausadas até às **{until_str}**.",
            ephemeral=False
        )

        if self.on_action:
            await self.on_action(self.assignment_id, "postponed", interaction)

    async def cancel_button(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True

        state = DaemonState()
        state.cancel_assignment(self.assignment_id)

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.red()
            embed.set_footer(text=f"Status: ❌ Envio cancelado definitivamente por {interaction.user.name}.")

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            content=f"❌ **Cancelado:** A tarefa foi descartada. O robô não enviará e não emitirá mais alertas sobre ela.",
            ephemeral=False
        )

        if self.on_action:
            await self.on_action(self.assignment_id, "cancelled", interaction)


class MoodleBotClient(commands.Bot):
    """Bot do Discord com sincronização instantânea de Slash Commands por servidor e prefixos."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        console.print("[cyan]Sincronizando Slash Commands globalmente...[/cyan]")
        try:
            synced = await self.tree.sync()
            console.print(f"[green]✔ {len(synced)} Slash Commands sincronizados globalmente![/green]")
        except Exception as sync_err:
            console.print(f"[yellow]Nota na sincronização global: {sync_err}[/yellow]")

    async def on_ready(self):
        console.print(f"[bold green]✔ Bot conectado ao Discord como {self.user} (ID: {self.user.id})![/bold green]")
        for guild in self.guilds:
            try:
                # Remove comandos residuais no escopo do servidor para eliminar duplicatas no Discord,
                # mantendo apenas os comandos globais sincronizados no setup_hook.
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                console.print(f"[green]✔ Comandos do servidor '{guild.name}' consolidados (duplicatas removidas)![/green]")
            except Exception as e:
                console.print(f"[yellow]Aviso ao consolidar comandos no servidor {guild.name}: {e}[/yellow]")


# Instância global do Bot
bot = MoodleBotClient()


def build_tarefas_embed(disciplina: Optional[str] = None) -> discord.Embed:
    """Gera o painel visual das atividades e questionários cadastrados no Moodle."""
    state = DaemonState()
    assignments = state.data.get("assignments", {})

    if not assignments:
        return discord.Embed(
            title="📚 Painel de Atividades - Moodle UFMG",
            description="📋 Nenhuma atividade cadastrada no momento. O robô varre o Moodle periodicamente.",
            color=discord.Color.blue()
        )

    filtered_items = []
    norm_disc = normalize_text(disciplina) if disciplina else None
    for assign_id, item in assignments.items():
        if norm_disc:
            course_name = normalize_text(item.get("course", ""))
            title_name = normalize_text(item.get("title", ""))
            if norm_disc not in course_name and norm_disc not in title_name:
                continue
        filtered_items.append((assign_id, item))

    title_suffix = f" ({disciplina})" if disciplina else ""
    embed = discord.Embed(
        title=f"📚 Painel de Atividades - Moodle UFMG{title_suffix}",
        description=f"Total de itens monitorados: **{len(filtered_items)}**",
        color=discord.Color.blue()
    )

    trabalhos_pendentes = []
    quizzes_pendentes = []
    concluidas = []

    for assign_id, item in filtered_items:
        title = item.get("title", "Atividade")
        course = item.get("course", "Disciplina")
        due = item.get("due_date") or "Sem prazo"
        status = item.get("status", "")
        act_type = item.get("activity_type", "assign")
        is_sub = item.get("is_submitted", False)

        line = f"• **[{title}]({item.get('url', '')})**\n  🏫 {course} | ⏰ {due}"

        if is_sub or status == "submitted":
            concluidas.append(line)
        elif status == "cancelled":
            concluidas.append(f"{line} *(Cancelado)*")
        elif act_type == "quiz":
            quizzes_pendentes.append(line)
        else:
            trabalhos_pendentes.append(line)

    if trabalhos_pendentes:
        embed.add_field(
            name="📌 Trabalhos e Relatórios Pendentes (Envio de Arquivo)",
            value="\n".join(trabalhos_pendentes[:8]),
            inline=False
        )

    if quizzes_pendentes:
        count = len(quizzes_pendentes)
        sample = "\n".join(quizzes_pendentes[:6])
        if count > 6:
            sample += f"\n*... e mais {count - 6} questionários pendentes. Use `!tarefas ingles` para filtrar!*"
        embed.add_field(
            name=f"📝 Questionários & Quizzes Pendentes ({count})",
            value=sample,
            inline=False
        )

    if not trabalhos_pendentes and not quizzes_pendentes:
        embed.add_field(
            name="⏳ Atividades Pendentes",
            value="🎉 Nenhuma atividade pendente! Tudo em dia.",
            inline=False
        )

    if concluidas:
        count = len(concluidas)
        sample = "\n".join(concluidas[:6])
        if count > 6:
            sample += f"\n*... e mais {count - 6} atividades concluídas.*"
        embed.add_field(
            name=f"✅ Atividades Concluídas ({count})",
            value=sample,
            inline=False
        )

    embed.set_footer(text=f"Moodle AI Assistant • UFMG Virtual • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    return embed


async def build_status_embed() -> discord.Embed:
    """Gera o painel de telemetria e diagnóstico do assistente."""
    auth = MoodleAuth()
    is_valid, user = await auth.validate_session()

    courses = get_available_courses()
    mat_dir = settings.STORAGE_MATERIALS_DIR
    total_files = sum(len(list(p.glob("*.*"))) for p in mat_dir.iterdir() if p.is_dir()) if mat_dir.exists() else 0

    state = DaemonState()
    assignments = state.data.get("assignments", {})
    quizzes = [a for a in assignments.values() if a.get("activity_type") == "quiz"]
    assigns = [a for a in assignments.values() if a.get("activity_type") != "quiz"]

    embed = discord.Embed(
        title="🛰️ Telemetria & Status - Moodle AI Assistant",
        color=discord.Color.green() if is_valid else discord.Color.red()
    )

    embed.add_field(
        name="🔐 Sessão Moodle / MinhaUFMG",
        value=f"{'🟢 **Ativa & Headless**' if is_valid else '🔴 **Inativa/Expirada**'}\nUsuário: `{user or 'N/A'}`",
        inline=False
    )

    embed.add_field(
        name="📚 Base de Conhecimento (Materiais)",
        value=f"• Disciplinas ativas: **{len(courses)}**\n• Arquivos catalogados: **{total_files}**",
        inline=True
    )

    embed.add_field(
        name="📋 Atividades Moodle",
        value=f"• Trabalhos de envio: **{len(assigns)}**\n• Quizzes avaliativos: **{len(quizzes)}**",
        inline=True
    )

    embed.add_field(
        name="🧠 Hierarquia de Modelos IA",
        value=(
            f"1️⃣ Primário: `{settings.GEMINI_MODEL}`\n"
            f"2️⃣ Fallback 1: `{settings.GEMINI_FALLBACK_MODEL_1}`\n"
            f"3️⃣ Fallback 2: `{settings.GEMINI_FALLBACK_MODEL_2}`"
        ),
        inline=False
    )

    embed.set_footer(text=f"Daemon a cada {settings.CHECK_INTERVAL_MINUTES} min • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    return embed


def get_materiais_payload(disciplina: str):
    """Localiza materiais em storage/materials/ e empacota para envio no Discord."""
    mat_dir = settings.STORAGE_MATERIALS_DIR / sanitize_filename(disciplina)
    if not mat_dir.exists():
        # Busca aproximada tolerante a acentos por nome da disciplina
        norm_disc = normalize_text(disciplina)
        candidates = [p for p in settings.STORAGE_MATERIALS_DIR.iterdir() if p.is_dir() and norm_disc in normalize_text(p.name)]
        if candidates:
            mat_dir = candidates[0]
            disciplina = mat_dir.name
        else:
            return None, f"❌ Disciplina `{disciplina}` não encontrada em `storage/materials/`.", []

    files = [p for p in mat_dir.iterdir() if p.is_file() and p.suffix.lower() in [".pdf", ".csv", ".docx", ".zip"]]
    if not files:
        return None, f"📚 Nenhum material baixado encontrado para `{disciplina}`.", []

    embed = discord.Embed(
        title=f"📖 Materiais de Estudo: {disciplina}",
        description=f"Total de arquivos disponíveis: **{len(files)}**\nEnviando os principais materiais abaixo:",
        color=discord.Color.green()
    )

    discord_files = []
    file_lines = []
    for f in files[:5]:
        size_kb = f.stat().st_size // 1024
        file_lines.append(f"• `{f.name}` ({size_kb} KB)")
        if f.stat().st_size <= 25 * 1024 * 1024:
            discord_files.append(discord.File(str(f), filename=f.name))

    embed.add_field(name="Arquivos Anexados", value="\n".join(file_lines), inline=False)
    return embed, None, discord_files


# ----------------------------------------------------
# 1. Comandos de Barra (Slash Commands)
# ----------------------------------------------------

@bot.tree.command(name="tarefas", description="Lista as atividades do Moodle (pendentes e já entregues)")
@app_commands.describe(disciplina="Filtrar por disciplina (opcional)")
@app_commands.autocomplete(disciplina=course_autocomplete)
async def cmd_tarefas(interaction: discord.Interaction, disciplina: Optional[str] = None):
    await interaction.response.defer(ephemeral=False)
    embed = build_tarefas_embed(disciplina=disciplina)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="materiais", description="Envia no chat slides e materiais de estudo de uma disciplina")
@app_commands.describe(disciplina="Nome da disciplina cadastrada")
@app_commands.autocomplete(disciplina=course_autocomplete)
async def cmd_materiais(interaction: discord.Interaction, disciplina: str):
    await interaction.response.defer(ephemeral=False)
    embed, err, discord_files = get_materiais_payload(disciplina)
    if err:
        await interaction.followup.send(err)
    else:
        await interaction.followup.send(embed=embed, files=discord_files)


def format_reference_materials_msg(course_name: str, extra_files: List[Path]) -> str:
    """Gera texto informativo claro sobre os arquivos que a IA utilizará como referência."""
    dest_dir = settings.STORAGE_MATERIALS_DIR / sanitize_filename(course_name)
    stored_files = []
    if dest_dir.exists():
        stored_files = [p.name for p in dest_dir.iterdir() if p.is_file() and p.suffix.lower() in [".pdf", ".csv", ".docx", ".txt", ".zip"]]

    extra_names = [p.name for p in extra_files]
    lines = []
    if extra_names:
        lines.append(f"📎 **Arquivo(s) enviado(s) como referência por você:** {', '.join(f'`{n}`' for n in extra_names)}")
    if stored_files:
        lines.append(f"📚 **Material da disciplina encontrado em `storage/materials/`:** {', '.join(f'`{n}`' for n in stored_files[:3])}{' (e outros)' if len(stored_files) > 3 else ''}")
    if not extra_names and not stored_files:
        lines.append("ℹ️ **Nenhum arquivo externo anexado ou catalogado.** A IA resolverá com base estritamente no enunciado e nas questões extraídos diretamente do Moodle.")
    return "\n".join(lines)


async def _execute_solve_flow(
    send_func: Callable[[str], asyncio.Future],
    tarefa: str,
    instrucoes: Optional[str] = None,
    extra_files: Optional[List[Path]] = None,
    is_refazer: bool = False
):
    state = DaemonState()
    assignments = state.data.get("assignments", {})

    target_item = None
    if tarefa in assignments:
        target_item = assignments[tarefa]
    else:
        norm_tarefa = normalize_text(tarefa)
        for aid, item in assignments.items():
            if norm_tarefa in normalize_text(item.get("title", "")) or tarefa.lower() in item.get("url", "").lower():
                target_item = item
                break

    if not target_item:
        target_item = {
            "id": "custom_" + str(int(datetime.now().timestamp())),
            "title": tarefa,
            "course": "Geral / Sob Demanda",
            "url": tarefa if "http" in tarefa else settings.MOODLE_BASE_URL,
            "due_date": "Sob demanda",
            "time_remaining": "N/A"
        }

    solver = GeminiSolver()
    if not solver.client:
        await send_func("❌ Chave `GEMINI_API_KEY` não configurada no `.env`.")
        return

    assign_obj = Assignment(
        id=target_item.get("id", "1"),
        course_id="",
        course_name=target_item.get("course", "Geral"),
        title=target_item.get("title", tarefa),
        url=target_item.get("url", ""),
        description=target_item.get("description", ""),
        activity_type=target_item.get("activity_type", "quiz" if "mod/quiz" in target_item.get("url", "") else "assign")
    )

    try:
        ref_msg = format_reference_materials_msg(assign_obj.course_name, extra_files or [])
        action_verb = "🔄 Refazendo" if is_refazer else "🧠 Analisando"
        await send_func(f"{action_verb} **{assign_obj.title}**...\n{ref_msg}")

        if assign_obj.activity_type == "quiz":
            from src.scraper.moodle_quiz import MoodleQuizAutomator
            quiz_automator = MoodleQuizAutomator()
            ext_res = await quiz_automator.inspect_and_extract_quiz(assign_obj.url)
            if ext_res.get("success") and ext_res.get("questions"):
                draft = await solver.solve_quiz_with_live_context(
                    assignment=assign_obj,
                    questions_data=ext_res["questions"],
                    user_notes=instrucoes,
                    extra_context_files=extra_files
                )
            else:
                draft = await solver.solve_assignment(
                    assignment=assign_obj,
                    user_notes=instrucoes,
                    extra_context_files=extra_files
                )
        else:
            draft = await solver.solve_assignment(
                assignment=assign_obj,
                user_notes=instrucoes,
                extra_context_files=extra_files
            )

        notifier = MoodleDiscordNotifier()
        await notifier.send_assignment_review(assign_obj, draft)

    except Exception as e:
        await send_func(f"❌ Erro ao gerar resolução: {e}")


@bot.tree.command(name="resolver", description="Resolve uma tarefa ou questionário pendente com IA")
@app_commands.describe(
    tarefa="ID, link ou nome da tarefa pendente a ser resolvida",
    instrucoes="Instruções adicionais personalizadas (ex: use linguagem R ou deduza passo a passo)",
    arquivo="Arquivo de referência complementar (enunciado, foto ou PDF)"
)
@app_commands.autocomplete(tarefa=pending_task_autocomplete)
async def cmd_resolver(
    interaction: discord.Interaction,
    tarefa: str,
    instrucoes: Optional[str] = None,
    arquivo: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=False)
    extra_files = []
    if arquivo:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest_file = temp_dir / sanitize_filename(arquivo.filename)
        await arquivo.save(dest_file)
        extra_files.append(dest_file)

    await _execute_solve_flow(
        send_func=interaction.followup.send,
        tarefa=tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=False
    )


@bot.tree.command(name="refazer", description="Refaz uma tarefa ou questionário já concluído com IA")
@app_commands.describe(
    tarefa="ID, link ou nome da tarefa concluída a ser refeita",
    instrucoes="Novas instruções ou ajustes desejados (ex: refazer questão 2 com mais detalhes)",
    arquivo="Arquivo de referência complementar (enunciado, foto ou PDF)"
)
@app_commands.autocomplete(tarefa=completed_task_autocomplete)
async def cmd_refazer(
    interaction: discord.Interaction,
    tarefa: str,
    instrucoes: Optional[str] = None,
    arquivo: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=False)
    extra_files = []
    if arquivo:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest_file = temp_dir / sanitize_filename(arquivo.filename)
        await arquivo.save(dest_file)
        extra_files.append(dest_file)

    await _execute_solve_flow(
        send_func=interaction.followup.send,
        tarefa=tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=True
    )


@bot.tree.command(name="status", description="Exibe o status da sessão Moodle, materiais e modelos de IA")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    embed = await build_status_embed()
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="adicionarconteudo", description="Salva arquivos e resumos na base de conhecimento da matéria")
@app_commands.describe(
    disciplina="Disciplina que receberá o material",
    arquivo="Arquivo a ser salvo (PDF, resumo, slide, lista)"
)
@app_commands.autocomplete(disciplina=course_autocomplete)
async def cmd_adicionarconteudo(
    interaction: discord.Interaction,
    disciplina: str,
    arquivo: discord.Attachment
):
    await interaction.response.defer(ephemeral=False)

    content_ch_id = settings.DISCORD_CONTENT_CHANNEL_ID
    if content_ch_id and content_ch_id != 0 and interaction.channel_id != content_ch_id:
        await interaction.followup.send(
            f"⚠️ Este comando deve ser executado no canal dedicado a conteúdos: <#{content_ch_id}>.",
            ephemeral=True
        )
        return

    dest_dir = settings.STORAGE_MATERIALS_DIR / sanitize_filename(disciplina)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_file = dest_dir / sanitize_filename(arquivo.filename)
    await arquivo.save(dest_file)

    console.print(f"[green]✔ Novo conteúdo adicionado via Discord:[/green] {dest_file.name} em {disciplina}")

    embed = discord.Embed(
        title="📥 Conteúdo Adicionado à Base de Conhecimento!",
        description=f"O arquivo **`{arquivo.filename}`** foi salvo com sucesso.",
        color=discord.Color.green()
    )
    embed.add_field(name="🏫 Disciplina", value=disciplina, inline=True)
    embed.add_field(name="📦 Tamanho", value=f"{arquivo.size // 1024} KB", inline=True)
    embed.set_footer(text="A IA passará a considerar este documento nas próximas resoluções.")

    await interaction.followup.send(embed=embed)


# ----------------------------------------------------
# 2. Comandos de Mensagem / Prefixo (!tarefas, !status, etc.)
# ----------------------------------------------------

@bot.command(name="tarefas")
async def prefix_tarefas(ctx: commands.Context, *, disciplina: Optional[str] = None):
    """Comando alternativo com prefixo: !tarefas [disciplina]."""
    embed = build_tarefas_embed(disciplina=disciplina)
    await ctx.send(embed=embed)


@bot.command(name="status")
async def prefix_status(ctx: commands.Context):
    """Comando alternativo com prefixo: !status."""
    embed = await build_status_embed()
    await ctx.send(embed=embed)


@bot.command(name="materiais")
async def prefix_materiais(ctx: commands.Context, *, disciplina: str):
    """Comando alternativo com prefixo: !materiais <disciplina>."""
    embed, err, discord_files = get_materiais_payload(disciplina)
    if err:
        await ctx.send(err)
    else:
        await ctx.send(embed=embed, files=discord_files)


@bot.command(name="resolver")
async def prefix_resolver(ctx: commands.Context, tarefa: str, *, instrucoes: Optional[str] = None):
    """Comando alternativo com prefixo: !resolver <id_ou_nome> [instruções]."""
    extra_files = []
    if ctx.message.attachments:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        for att in ctx.message.attachments:
            dest_file = temp_dir / sanitize_filename(att.filename)
            await att.save(dest_file)
            extra_files.append(dest_file)

    await _execute_solve_flow(
        send_func=ctx.send,
        tarefa=tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=False
    )


@bot.command(name="refazer")
async def prefix_refazer(ctx: commands.Context, tarefa: str, *, instrucoes: Optional[str] = None):
    """Comando alternativo com prefixo: !refazer <id_ou_nome> [instruções]."""
    extra_files = []
    if ctx.message.attachments:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        for att in ctx.message.attachments:
            dest_file = temp_dir / sanitize_filename(att.filename)
            await att.save(dest_file)
            extra_files.append(dest_file)

    await _execute_solve_flow(
        send_func=ctx.send,
        tarefa=tarefa,
        instrucoes=instrucoes,
        extra_files=extra_files,
        is_refazer=True
    )


@bot.command(name="adicionarconteudo")
async def prefix_adicionarconteudo(ctx: commands.Context, *, disciplina: str):
    """Comando alternativo com prefixo: !adicionarconteudo <disciplina> (anexe arquivo)."""
    content_ch_id = settings.DISCORD_CONTENT_CHANNEL_ID
    if content_ch_id and content_ch_id != 0 and ctx.channel.id != content_ch_id:
        await ctx.send(f"⚠️ Este comando deve ser executado no canal dedicado a conteúdos: <#{content_ch_id}>.")
        return

    if not ctx.message.attachments:
        await ctx.send("⚠️ Por favor, anexe o arquivo (PDF, slide, resumo) junto com o comando `!adicionarconteudo <disciplina>`.")
        return

    dest_dir = settings.STORAGE_MATERIALS_DIR / sanitize_filename(disciplina)
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved_names = []
    for att in ctx.message.attachments:
        dest_file = dest_dir / sanitize_filename(att.filename)
        await att.save(dest_file)
        saved_names.append(dest_file.name)

    embed = discord.Embed(
        title="📥 Conteúdo Adicionado à Base de Conhecimento!",
        description=f"Os arquivos **{', '.join(saved_names)}** foram salvos com sucesso.",
        color=discord.Color.green()
    )
    embed.add_field(name="🏫 Disciplina", value=disciplina, inline=True)
    embed.set_footer(text="A IA passará a considerar este documento nas próximas resoluções.")
    await ctx.send(embed=embed)


@bot.command(name="ajuda")
async def prefix_ajuda(ctx: commands.Context):
    """Exibe o guia de comandos do robô."""
    embed = discord.Embed(
        title="🤖 Moodle AI Assistant - Comandos Disponíveis",
        description="Você pode interagir usando comandos de barra (`/`) ou prefixo (`!`):",
        color=discord.Color.blue()
    )
    embed.add_field(name="📋 `!tarefas` ou `/tarefas [disciplina]`", value="Lista tarefas e questionários pendentes e concluídos.", inline=False)
    embed.add_field(name="📖 `!materiais <disciplina>` ou `/materiais`", value="Envia slides e materiais de estudo no chat.", inline=False)
    embed.add_field(name="🧠 `!resolver <id_ou_nome>` ou `/resolver`", value="Resolve atividade ou questionário pendente sob demanda com IA.", inline=False)
    embed.add_field(name="🔄 `!refazer <id_ou_nome>` ou `/refazer`", value="Refaz atividade ou questionário já concluído com IA.", inline=False)
    embed.add_field(name="🛰️ `!status` ou `/status`", value="Exibe a sessão do Moodle, materiais e IA.", inline=False)
    embed.add_field(name="📥 `!adicionarconteudo <disciplina>` (com anexo)", value="Salva resumos e materiais na memória da IA.", inline=False)
    await ctx.send(embed=embed)


class MoodleDiscordNotifier:
    """Cliente unificado do Discord para notificações de rascunhos, alertas e notas."""

    def __init__(self, token: Optional[str] = None, channel_id: Optional[int] = None):
        self.token = token or settings.DISCORD_BOT_TOKEN
        self.channel_id = channel_id or settings.DISCORD_CHANNEL_ID

    async def _resolve_channel(self) -> Optional[discord.abc.Messageable]:
        """Obtém o canal do Discord com fallback seguro para fetch_channel."""
        if not bot.is_ready():
            if self.token:
                try:
                    await asyncio.wait_for(bot.wait_until_ready(), timeout=5.0)
                except Exception:
                    pass
        channel = bot.get_channel(self.channel_id)
        if not channel and bot.is_ready():
            try:
                channel = await bot.fetch_channel(self.channel_id)
            except Exception:
                pass
        return channel

    async def send_assignment_review(
        self,
        assignment: Assignment,
        draft: SolutionDraft
    ) -> bool:
        """Envia o rascunho de resolução (PDF e Markdown) para o canal privado com botões."""
        if not self.token or self.token == "seu_discord_bot_token_aqui":
            console.print("[yellow]Aviso: DISCORD_BOT_TOKEN não configurado no .env.[/yellow]")
            return False

        if not self.channel_id or self.channel_id == 0:
            console.print("[yellow]Aviso: DISCORD_CHANNEL_ID não configurado no .env.[/yellow]")
            return False

        sent_success = False
        file_to_send = draft.pdf_path if (draft.pdf_path and draft.pdf_path.exists()) else draft.output_path

        act_type = getattr(assignment, "activity_type", "assign")
        type_str = "Questionário Online" if act_type == "quiz" else "Trabalho Acadêmico"

        try:
            channel = await self._resolve_channel()

            if channel:
                if act_type == "quiz":
                    embed_desc = (
                        "As respostas para o questionário online foram preparadas pela IA.\n\n"
                        "• **[📝 Apenas Preencher Quiz]**: Digita as respostas no Moodle e salva na tentativa sem submeter. Você poderá abrir o Moodle e conferir!\n"
                        "• **[🚀 Enviar Tudo e Terminar]**: Finaliza a tentativa e confirma o envio no Moodle."
                    )
                else:
                    embed_desc = (
                        f"As respostas foram preparadas para sua conferência ({type_str}).\n"
                        "Leia o documento anexado abaixo e confirme o envio usando os botões."
                    )

                embed = discord.Embed(
                    title=f"📋 Revisão: {assignment.title}",
                    url=assignment.url,
                    description=embed_desc,
                    color=discord.Color.blue()
                )

                embed.add_field(name="🏫 Disciplina", value=assignment.course_name, inline=False)
                embed.add_field(name="⏰ Prazo de Entrega", value=assignment.due_date_str or "Não especificado", inline=True)
                embed.add_field(name="⏳ Tempo Restante", value=assignment.time_remaining or "N/A", inline=True)
                embed.add_field(name="🧠 Modelo Utilizado", value=f"`{draft.used_model}`", inline=True)

                summary_text = draft.summary[:800] + ("..." if len(draft.summary) > 800 else "")
                embed.add_field(name="📝 Respostas Preparadas", value=f"```markdown\n{summary_text}\n```", inline=False)

                if draft.used_materials:
                    embed.add_field(
                        name="📚 Fontes & Materiais de Referência",
                        value="\n".join(f"• `{m}`" for m in draft.used_materials[:5]),
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="📚 Fontes & Materiais de Referência",
                        value="ℹ️ Nenhum arquivo externo utilizado. A resolução foi baseada exclusivamente no enunciado e questões extraídos diretamente do Moodle.",
                        inline=False
                    )

                if assignment.url:
                    embed.add_field(
                        name="🔗 Acesso Direto",
                        value=f"[Abrir Atividade no Moodle UFMG]({assignment.url})",
                        inline=False
                    )

                embed.set_footer(text="Ação humana obrigatória • Clique abaixo para submeter")

                discord_file = discord.File(str(file_to_send), filename=file_to_send.name)
                view = ReviewActionView(
                    assignment_id=assignment.id,
                    assignment_url=assignment.url,
                    file_to_submit=file_to_send,
                    activity_type=act_type,
                    structured_answers=draft.structured_answers
                )

                await channel.send(
                    content=f"🔔 **Nova resolução pronta para revisão:** `{assignment.title}`",
                    embed=embed,
                    file=discord_file,
                    view=view
                )
                console.print(f"[green]✔ Card de revisão com anexo {file_to_send.name} enviado no Discord![/green]")
                return True

        except Exception as e:
            console.print(f"[red]Erro ao enviar mensagem no Discord via bot global: {e}[/red]")

        return sent_success

    async def send_grade_notification(
        self,
        course_name: str,
        assignment_title: str,
        grade: str,
        feedback: Optional[str] = None,
        graded_by: Optional[str] = None
    ) -> bool:
        """Envia aviso festivo quando o professor publica a nota e correção no Moodle."""
        try:
            channel = await self._resolve_channel()
            if channel:
                embed = discord.Embed(
                    title=f"🎉 Nota Publicada: {assignment_title}",
                    description=f"O professor avaliou sua atividade na disciplina **{course_name}**!",
                    color=discord.Color.green()
                )
                embed.add_field(name="📊 Nota Atribuída", value=f"**{grade}**", inline=True)
                if graded_by:
                    embed.add_field(name="👨‍🏫 Avaliador", value=graded_by, inline=True)
                if feedback:
                    embed.add_field(name="💬 Feedback / Comentários do Professor", value=f"```\n{feedback}\n```", inline=False)

                embed.set_footer(text=f"UFMG Virtual • {datetime.now().strftime('%d/%m/%Y %H:%M')}")
                await channel.send(content="🔔 **Resultado de Avaliação Disponível!**", embed=embed)
                return True
        except Exception as e:
            console.print(f"[red]Erro ao enviar notificação de nota: {e}[/red]")
        return False

    async def send_course_announcement(
        self,
        announcement
    ) -> bool:
        """Envia comunicado/aviso da turma publicado pelo professor para o Discord."""
        try:
            target_ch_id = settings.DISCORD_ANNOUNCEMENTS_CHANNEL_ID or self.channel_id
            if not target_ch_id or target_ch_id == 0:
                target_ch_id = self.channel_id

            if not bot.is_ready():
                if self.token:
                    try:
                        await asyncio.wait_for(bot.wait_until_ready(), timeout=5.0)
                    except Exception:
                        pass

            channel = bot.get_channel(target_ch_id)
            if not channel and bot.is_ready():
                try:
                    channel = await bot.fetch_channel(target_ch_id)
                except Exception:
                    pass

            if not channel:
                channel = await self._resolve_channel()

            if channel:
                embed = discord.Embed(
                    title=f"📢 Novo Aviso: {announcement.title}",
                    url=announcement.url,
                    color=discord.Color.gold()
                )
                embed.add_field(name="🏫 Disciplina", value=announcement.course_name, inline=False)
                if announcement.author:
                    embed.add_field(name="👤 Publicado por", value=announcement.author, inline=True)
                if announcement.date:
                    embed.add_field(name="📅 Data / Hora", value=announcement.date, inline=True)

                msg_content = announcement.message.strip() if announcement.message else "Clique no link abaixo para ler o comunicado completo no Moodle."
                if len(msg_content) > 1800:
                    msg_content = msg_content[:1800] + "\n\n*(Mensagem longa truncada - abra no Moodle para ler na íntegra)*"

                embed.description = msg_content
                embed.set_footer(text=f"Moodle UFMG • {datetime.now().strftime('%d/%m/%Y %H:%M')}")

                view = ui.View(timeout=None)
                view.add_item(ui.Button(
                    label="Abrir Aviso no Moodle",
                    style=discord.ButtonStyle.link,
                    url=announcement.url,
                    emoji="🔗"
                ))

                await channel.send(
                    content=f"📣 **Aviso da Turma:** Novo comunicado em **{announcement.course_name}**!",
                    embed=embed,
                    view=view
                )
                return True
        except Exception as e:
            console.print(f"[red]Erro ao enviar comunicado da turma: {e}[/red]")
        return False

    async def send_countdown_alert(
        self,
        title: str,
        course_name: str,
        minutes_remaining: int
    ) -> bool:
        """Envia alertas intensivos de contagem regressiva para prazos próximos."""
        try:
            channel = await self._resolve_channel()
            if channel:
                if minutes_remaining <= 1:
                    content = "@everyone 🚨🚨 **ÚLTIMO MINUTO! PRAZO ENCERRANDO!** 🚨🚨"
                    color = discord.Color.dark_red()
                    title_text = f"🔥 MENOS DE 60 SEGUNDOS: {title}"
                    desc = (
                        f"O prazo da atividade **{title}** ({course_name}) encerra em **menos de 1 minuto**!\n"
                        f"Abra o card acima e clique em **[✅ Aprovar e Enviar]** IMEDIATAMENTE ou envie você mesmo no Moodle!"
                    )
                elif minutes_remaining <= 5:
                    content = "@everyone ⏰ **ALERTA DE URGÊNCIA (5 MINUTOS)!**"
                    color = discord.Color.red()
                    title_text = f"🚨 FALTAM 5 MINUTOS: {title}"
                    desc = (
                        f"Faltam apenas **5 minutos** para o vencimento de **{title}** ({course_name})!\n"
                        f"Confira o PDF anexado acima e aprove o envio se desejar entregar."
                    )
                else:
                    content = f"⚠️ **Atenção:** Prazo de entrega se aproximando ({minutes_remaining} min)!"
                    color = discord.Color.orange()
                    title_text = f"⚠️ Faltam {minutes_remaining} minutos: {title}"
                    desc = f"A atividade **{title}** ({course_name}) encerra em {minutes_remaining} minutos."

                embed = discord.Embed(title=title_text, description=desc, color=color)
                await channel.send(content=content, embed=embed)
                return True
        except Exception as e:
            console.print(f"[red]Erro ao enviar alerta de contagem: {e}[/red]")
        return False


async def run_bot():
    """Inicia o Bot do Discord com os Slash Commands ativos."""
    if not settings.DISCORD_BOT_TOKEN:
        console.print("[red]Erro: DISCORD_BOT_TOKEN não configurado.[/red]")
        return
    await bot.start(settings.DISCORD_BOT_TOKEN)


def main():
    parser = argparse.ArgumentParser(description="Moodle Discord Bot (Slash Commands)")
    parser.add_argument("--run", action="store_true", help="Inicia o bot e registra os Slash Commands")
    args = parser.parse_args()

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
