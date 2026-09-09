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


def normalize_text(text: str) -> str:
    """Remove acentos e padroniza para buscas tolerantes."""
    return unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("utf-8").lower()


def get_available_courses() -> List[str]:
    """Retorna os nomes das disciplinas que possuem pasta em storage/materials/."""
    mat_dir = settings.STORAGE_MATERIALS_DIR
    if not mat_dir.exists():
        return []
    return [p.name for p in mat_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]


async def course_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """Autocomplete interativo para seleção de disciplinas no Discord (tolerante a acentos)."""
    courses = get_available_courses()
    norm_curr = normalize_text(current)
    filtered = [c for c in courses if norm_curr in normalize_text(c)]
    return [
        app_commands.Choice(name=c[:100], value=c)
        for c in filtered[:25]
    ]


class ReviewActionView(ui.View):
    """Componente interativo com botões de decisão e persistência imediata em disco."""

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

        # Customiza rótulo e ícone do botão de aprovação conforme o tipo da atividade
        for child in self.children:
            if getattr(child, "custom_id", "") == "btn_approve":
                if self.activity_type == "quiz":
                    child.label = "Preencher e Enviar Quiz"
                    child.emoji = "📝"
                else:
                    child.label = "Aprovar e Enviar PDF"
                    child.emoji = "📄"

    @ui.button(label="Aprovar e Enviar", style=discord.ButtonStyle.success, emoji="✅", custom_id="btn_approve")
    async def approve_button(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        action_desc = "Preenchendo questionário no Moodle..." if self.activity_type == "quiz" else "Enviando arquivo no Moodle..."
        if embed:
            embed.color = discord.Color.gold()
            embed.set_footer(
                text=f"Status: 🔄 Aprovado por {interaction.user.name}! {action_desc}"
            )

        await interaction.response.edit_message(embed=embed, view=self)

        submitter = MoodleSubmitter()
        current_time = datetime.now().strftime("%H:%M:%S")

        if self.activity_type == "quiz":
            console.print(
                f"[bold cyan]Aprovação recebida no Discord para questionário {self.assignment_id}![/bold cyan] "
                f"Executando preenchimento com cadência humana no Moodle..."
            )
            await interaction.followup.send(
                content="⏳ **Preenchendo questionário no Moodle com simulação de leitura humana** (aguardando ~3 min para envio seguro e natural)...",
                ephemeral=False
            )
            ans_payload = {}
            if isinstance(self.structured_answers, list):
                for item in self.structured_answers:
                    if isinstance(item, dict) and "key" in item:
                        ans_payload[item["key"]] = item["value"]
            elif isinstance(self.structured_answers, dict):
                ans_payload = self.structured_answers

            success, message = await submitter.submit_quiz(
                quiz_url=self.assignment_url,
                answers=ans_payload or self.structured_answers
            )
        else:
            if not self.file_to_submit:
                return
            console.print(
                f"[bold cyan]Aprovação recebida no Discord para tarefa {self.assignment_id}![/bold cyan] "
                f"Disparando envio de {self.file_to_submit.name}..."
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
            await interaction.message.edit(embed=embed)
            await interaction.followup.send(
                content=f"🎉 **Confirmação de Envio no Moodle:** {message}",
                ephemeral=False
            )
        else:
            if embed:
                embed.color = discord.Color.red()
                embed.set_footer(
                    text=f"Falha no envio às {current_time}: {message[:100]}"
                )
            await interaction.message.edit(embed=embed)
            await interaction.followup.send(
                content=f"⚠️ **Falha no envio:** {message}",
                ephemeral=False
            )

        if self.on_action:
            await self.on_action(self.assignment_id, "approved", interaction)

    @ui.button(label="Adiar (+1h)", style=discord.ButtonStyle.secondary, emoji="⏱️", custom_id="btn_postpone")
    async def postpone_button(self, interaction: discord.Interaction, button: ui.Button):
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

    @ui.button(label="Cancelar / Não Enviar", style=discord.ButtonStyle.danger, emoji="❌", custom_id="btn_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
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
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                console.print(f"[green]✔ {len(synced)} Slash Commands sincronizados instantaneamente no servidor '{guild.name}' ({guild.id})![/green]")
            except Exception as e:
                console.print(f"[yellow]Aviso ao sincronizar comandos no servidor {guild.name}: {e}[/yellow]")


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


@bot.tree.command(name="resolver", description="Resolve ou refaz uma tarefa sob demanda com IA")
@app_commands.describe(
    tarefa="ID, link ou nome da tarefa a ser resolvida",
    instrucoes="Instruções adicionais personalizadas (ex: use linguagem R ou deduza passo a passo)",
    arquivo="Arquivo de referência complementar (enunciado, foto ou PDF)"
)
async def cmd_resolver(
    interaction: discord.Interaction,
    tarefa: str,
    instrucoes: Optional[str] = None,
    arquivo: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=False)

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

    extra_files = []
    if arquivo:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest_file = temp_dir / sanitize_filename(arquivo.filename)
        await arquivo.save(dest_file)
        extra_files.append(dest_file)

    solver = GeminiSolver()
    if not solver.client:
        await interaction.followup.send("❌ Chave `GEMINI_API_KEY` não configurada no `.env`.")
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
        await interaction.followup.send(f"🧠 Analisando **{assign_obj.title}**...")
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
        await interaction.followup.send(f"❌ Erro ao gerar resolução: {e}")


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

    extra_files = []
    if ctx.message.attachments:
        temp_dir = Path("storage/submissions/temp_uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        for att in ctx.message.attachments:
            dest_file = temp_dir / sanitize_filename(att.filename)
            await att.save(dest_file)
            extra_files.append(dest_file)

    solver = GeminiSolver()
    if not solver.client:
        await ctx.send("❌ Chave `GEMINI_API_KEY` não configurada no `.env`.")
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
        await ctx.send(f"🧠 Analisando **{assign_obj.title}**...")
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
        await ctx.send(f"❌ Erro ao gerar resolução: {e}")


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
    embed.add_field(name="🧠 `!resolver <id_ou_nome> [instruções]` ou `/resolver`", value="Resolve atividade ou questionário sob demanda com IA.", inline=False)
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
                embed = discord.Embed(
                    title=f"📋 Revisão: {assignment.title}",
                    url=assignment.url,
                    description=(
                        f"As respostas foram preparadas para sua conferência ({type_str}).\n"
                        "Leia o documento anexado abaixo e confirme o envio usando os botões."
                    ),
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
                        name="📚 Contexto Utilizado (Slides & Materiais)",
                        value="\n".join(f"• `{m}`" for m in draft.used_materials[:5]),
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
