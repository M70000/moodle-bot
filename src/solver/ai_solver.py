"""Camada de abstração multi-provider para resolução de atividades com IA.

Suporta três provedores BYOK:
  - Google Gemini  (GEMINI_API_KEY)    → google-genai SDK
  - Anthropic Claude (ANTHROPIC_API_KEY) → anthropic SDK
  - DeepSeek         (DEEPSEEK_API_KEY)  → openai SDK com base_url customizada

O provedor ativo é detectado automaticamente pela chave disponível no .env.
Prioridade: Gemini > Claude > DeepSeek (primeira chave válida vence).

Uso interno: todos os comandos Discord e o daemon usam `AISolver` em vez de
chamar `GeminiSolver` diretamente, garantindo que a chave BYOK do usuário
seja sempre respeitada independente do provedor configurado.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console

from config.settings import settings
from src.scraper.moodle_scraper import Assignment

console = Console()


def _detect_provider() -> str:
    """Detecta qual provedor está configurado pela chave disponível no .env."""
    if settings.GEMINI_API_KEY and settings.GEMINI_API_KEY not in ("", "sua_chave_gemini_api_aqui"):
        return "gemini"
    if settings.ANTHROPIC_API_KEY and settings.ANTHROPIC_API_KEY not in ("", "sua_chave_anthropic_aqui"):
        return "anthropic"
    if settings.DEEPSEEK_API_KEY and settings.DEEPSEEK_API_KEY not in ("", "sua_chave_deepseek_aqui"):
        return "deepseek"
    return "gemini"  # Fallback — GeminiSolver dará aviso de chave ausente


def get_active_provider() -> str:
    """Retorna o nome do provedor ativo para exibição no /status."""
    p = _detect_provider()
    if p == "anthropic":
        model = settings.ANTHROPIC_MODEL or "claude-haiku-4-5"
        return f"Anthropic Claude ({model})"
    if p == "deepseek":
        model = settings.DEEPSEEK_MODEL or "deepseek-chat"
        return f"DeepSeek ({model})"
    model = settings.GEMINI_MODEL or "gemini-3.5-flash"
    return f"Google Gemini ({model})"


async def _emit_log(callback: Optional[Any], msg: str):
    """Envia mensagem para o callback de log ao vivo de forma segura."""
    if not callback:
        return
    try:
        res = callback(msg)
        if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
            await res
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Claude Backend
# ---------------------------------------------------------------------------

async def _call_claude(
    system_instruction: str,
    user_message: str,
    on_log: Optional[Any] = None,
) -> Tuple[str, str]:
    """Chama a API do Anthropic Claude e retorna (texto, modelo_usado)."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "SDK do Anthropic não instalado. Execute: pip install anthropic"
        )

    model = settings.ANTHROPIC_MODEL or "claude-haiku-4-5"
    await _emit_log(on_log, f"Consultando Anthropic Claude ({model})...")

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await asyncio.wait_for(
        client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_instruction,
            messages=[{"role": "user", "content": user_message}],
        ),
        timeout=120,
    )
    text = response.content[0].text if response.content else ""
    await _emit_log(on_log, f"✔ Resposta do Claude ({model}) recebida.")
    return text, model


# ---------------------------------------------------------------------------
# DeepSeek Backend
# ---------------------------------------------------------------------------

async def _call_deepseek(
    system_instruction: str,
    user_message: str,
    on_log: Optional[Any] = None,
) -> Tuple[str, str]:
    """Chama a API do DeepSeek (compatível com OpenAI) e retorna (texto, modelo_usado)."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError(
            "SDK da OpenAI não instalado (necessário para DeepSeek). Execute: pip install openai"
        )

    model = settings.DEEPSEEK_MODEL or "deepseek-chat"
    await _emit_log(on_log, f"Consultando DeepSeek ({model})...")

    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
    )
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_message},
            ],
            max_tokens=4096,
            temperature=0.1,
        ),
        timeout=120,
    )
    text = response.choices[0].message.content or ""
    await _emit_log(on_log, f"✔ Resposta do DeepSeek ({model}) recebida.")
    return text, model


# ---------------------------------------------------------------------------
# AISolver — Fachada Unificada
# ---------------------------------------------------------------------------

class AISolver:
    """Motor de resolução unificado com suporte a Gemini, Claude e DeepSeek.

    A lógica completa de resolução (prompts, parsing, geração de arquivo) é
    delegada ao GeminiSolver quando o provedor for Gemini.

    Para Claude e DeepSeek, este módulo constrói os prompts e chama os
    backends correspondentes, usando a mesma lógica de saída.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.provider = _detect_provider()
        self._gemini_solver = None

        if self.provider == "gemini" or not (
            settings.ANTHROPIC_API_KEY or settings.DEEPSEEK_API_KEY
        ):
            # Sempre cria o GeminiSolver como fallback final
            from src.solver.gemini_solver import GeminiSolver
            self._gemini_solver = GeminiSolver(api_key=api_key)

        console.print(
            f"[cyan]🤖 Provedor IA ativo: [bold]{get_active_provider()}[/bold][/cyan]"
        )

    async def solve_assignment(
        self,
        assignment: Assignment,
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None,
        on_log: Optional[Any] = None,
        auto_triggered: bool = False,
    ):
        """Gera resolução completa delegando ao backend ativo.

        Args:
            auto_triggered: Se True → gera PDF (daemon/emergência).
                            Se False → gera DOCX editável (resolução manual).
        """
        if self.provider == "gemini" or self._gemini_solver:
            return await self._gemini_solver.solve_assignment(
                assignment=assignment,
                user_notes=user_notes,
                extra_context_files=extra_context_files,
                on_log=on_log,
                auto_triggered=auto_triggered,
            )

        # Claude / DeepSeek: usa GeminiSolver como estrutura mas substitui a chamada AI
        from src.solver.gemini_solver import GeminiSolver, SolutionDraft
        from src.solver.gemini_solver import extract_text_from_context_files
        from src.scraper.moodle_scraper import sanitize_filename

        dummy = GeminiSolver.__new__(GeminiSolver)
        dummy.client = None
        dummy.materials_dir = settings.STORAGE_MATERIALS_DIR
        dummy.submissions_dir = settings.STORAGE_SUBMISSIONS_DIR

        context_files = []
        for att in assignment.attachments:
            if att.local_path and att.local_path.exists():
                context_files.append(att.local_path)
        if extra_context_files:
            context_files.extend([f for f in extra_context_files if f.exists()])

        system_instruction = (
            "Você é um estudante universitário da UFMG realizando esta atividade acadêmica. "
            "Resolva de forma clara, direta e acadêmica, sem texto introdutório de IA.\n\n"
            "FORMATO: Comece com ### Questão 1 e assim por diante. "
            "Ao final, adicione um bloco ```json:answers``` com as respostas estruturadas para quiz."
        )

        extracted_ref = extract_text_from_context_files(context_files)
        user_message = (
            f"DISCIPLINA: {assignment.course_name}\n"
            f"ATIVIDADE: {assignment.title}\n"
            f"ENUNCIADO:\n{assignment.description}\n"
        )
        if extracted_ref:
            user_message += f"\nMATERIAL DE APOIO:\n{extracted_ref}\n"
        if user_notes:
            user_message += f"\nINSTRUÇÕES DO ALUNO:\n{user_notes}\n"

        if self.provider == "anthropic":
            full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
        else:
            full_text, used_model = await _call_deepseek(system_instruction, user_message, on_log)

        return await _build_solution_draft(
            assignment=assignment,
            full_text=full_text,
            used_model=used_model,
            on_log=on_log,
            auto_triggered=auto_triggered,
        )

    async def solve_quiz_with_live_context(
        self,
        assignment: Assignment,
        questions_data: List[Dict[str, Any]],
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None,
        on_log: Optional[Any] = None,
        auto_triggered: bool = False,
    ):
        """Resolve questionário ao vivo delegando ao backend ativo."""
        if self.provider == "gemini" or self._gemini_solver:
            return await self._gemini_solver.solve_quiz_with_live_context(
                assignment=assignment,
                questions_data=questions_data,
                user_notes=user_notes,
                extra_context_files=extra_context_files,
                on_log=on_log,
                auto_triggered=auto_triggered,
            )

        # Claude / DeepSeek fallback
        from src.solver.gemini_solver import extract_text_from_context_files

        context_files = []
        if extra_context_files:
            context_files.extend([f for f in extra_context_files if f.exists()])

        formatted_questions = []
        for q in questions_data:
            q_txt = q.get("fullTextWithTokens", "").strip()
            if q_txt:
                formatted_questions.append(f"### {q.get('qNumberText', 'Questão')}\n{q_txt}")
        questions_body = "\n\n".join(formatted_questions)

        system_instruction = (
            "Você é um estudante universitário da UFMG realizando questionário no Moodle. "
            "Responda cada questão com letra e texto completo. "
            "Formate a saída com ```json:answers``` no início e folha de respostas depois."
        )

        extracted_ref = extract_text_from_context_files(context_files)
        user_message = (
            f"DISCIPLINA: {assignment.course_name}\n"
            f"ATIVIDADE: {assignment.title}\n\n"
            f"QUESTIONÁRIO:\n{questions_body}\n"
        )
        if extracted_ref:
            user_message += f"\nMATERIAL DE APOIO:\n{extracted_ref}\n"
        if user_notes:
            user_message += f"\nINSTRUÇÕES DO ALUNO:\n{user_notes}\n"

        if self.provider == "anthropic":
            full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
        else:
            full_text, used_model = await _call_deepseek(system_instruction, user_message, on_log)

        return await _build_solution_draft(
            assignment=assignment,
            full_text=full_text,
            used_model=used_model,
            on_log=on_log,
            auto_triggered=auto_triggered,
            is_quiz=True,
        )

    async def apply_revision(
        self,
        draft,
        revision_instructions: str,
        on_log: Optional[Any] = None,
    ):
        """Aplica revisão do usuário sobre um draft existente e gera novo DOCX.

        Args:
            draft: SolutionDraft existente com o conteúdo atual
            revision_instructions: Instruções de modificação do usuário (texto livre)
        """
        if self.provider == "gemini" or self._gemini_solver:
            return await self._gemini_solver.apply_revision(
                draft=draft,
                revision_instructions=revision_instructions,
                on_log=on_log,
            )

        system_instruction = (
            "Você é um assistente acadêmico. O aluno revisou a resolução abaixo e quer que você aplique as modificações. "
            "Mantenha o formato Markdown com ### Questão X. Aplique APENAS as mudanças pedidas, "
            "sem alterar o restante do conteúdo."
        )
        user_message = (
            f"RESOLUÇÃO ATUAL:\n{draft.full_markdown}\n\n"
            f"INSTRUÇÕES DE MODIFICAÇÃO DO ALUNO:\n{revision_instructions}\n\n"
            "Gere a resolução modificada completa."
        )

        if self.provider == "anthropic":
            full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
        else:
            full_text, used_model = await _call_deepseek(system_instruction, user_message, on_log)

        from src.scraper.moodle_scraper import Assignment as _Assign
        mock_assign = _Assign(
            id=draft.assignment_id,
            course_id="",
            course_name=draft.course_name,
            title=draft.assignment_title,
            url="",
            description="",
        )
        return await _build_solution_draft(
            assignment=mock_assign,
            full_text=full_text,
            used_model=used_model,
            on_log=on_log,
            auto_triggered=False,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_solution_draft(
    assignment: Assignment,
    full_text: str,
    used_model: str,
    on_log: Optional[Any],
    auto_triggered: bool,
    is_quiz: bool = False,
):
    """Constrói SolutionDraft a partir do texto bruto da IA, gerando PDF ou DOCX conforme contexto."""
    import json as _json
    from src.scraper.moodle_scraper import sanitize_filename
    from src.solver.gemini_solver import SolutionDraft

    # Extrai JSON:answers
    structured_answers = None
    json_match = re.search(r"```(?:json:answers|json)\s*\n(.*?)\n```", full_text, re.DOTALL)
    if json_match:
        try:
            parsed = _json.loads(json_match.group(1).strip())
            if isinstance(parsed, list):
                structured_answers = parsed
            elif isinstance(parsed, dict):
                structured_answers = [{"key": k, "value": v} for k, v in parsed.items()]
        except Exception:
            pass

    clean_markdown = re.sub(r"```(?:json:answers|json)\s*\n.*?\n```", "", full_text, flags=re.DOTALL).strip()
    summary_lines = [l for l in clean_markdown.splitlines() if l.strip() and not l.startswith("#")]
    summary = "\n".join(summary_lines[:6]) if summary_lines else clean_markdown[:400]

    safe_course = sanitize_filename(assignment.course_name)
    safe_title = sanitize_filename(assignment.title)
    dest_dir = settings.STORAGE_SUBMISSIONS_DIR / safe_course
    dest_dir.mkdir(parents=True, exist_ok=True)

    draft_path = dest_dir / f"{safe_title}_rascunho.md"
    draft_path.write_text(clean_markdown, encoding="utf-8")

    pdf_path = None
    docx_path = None

    if auto_triggered:
        # Resolução automática → gera PDF (comportamento original)
        pdf_path = dest_dir / f"{safe_title}.pdf"
        try:
            await _emit_log(on_log, "Compilando PDF acadêmico da resolução...")
            from src.solver.pdf_generator import AcademicPDFGenerator
            pdf_gen = AcademicPDFGenerator()
            await pdf_gen.render_pdf(
                markdown_text=clean_markdown,
                output_pdf_path=pdf_path,
                course_name=assignment.course_name,
                assignment_title=assignment.title,
            )
            await _emit_log(on_log, f"✔ PDF gerado: {pdf_path.name}")
        except Exception as e:
            console.print(f"[yellow]Aviso ao gerar PDF: {e}[/yellow]")
            pdf_path = None
    else:
        # Resolução manual → gera DOCX editável
        docx_path = dest_dir / f"{safe_title}.docx"
        try:
            await _emit_log(on_log, "Gerando documento Word editável (.docx)...")
            from src.solver.docx_generator import AcademicDocxGenerator
            docx_gen = AcademicDocxGenerator()
            ok = docx_gen.generate_docx(
                markdown_text=clean_markdown,
                output_path=docx_path,
                course_name=assignment.course_name,
                assignment_title=assignment.title,
            )
            if ok:
                await _emit_log(on_log, f"✔ DOCX editável gerado: {docx_path.name}")
            else:
                docx_path = None
        except Exception as e:
            console.print(f"[yellow]Aviso ao gerar DOCX: {e}[/yellow]")
            docx_path = None

    return SolutionDraft(
        assignment_id=assignment.id,
        assignment_title=assignment.title,
        course_name=assignment.course_name,
        summary=summary,
        full_markdown=full_text,
        output_path=draft_path,
        pdf_path=pdf_path,
        docx_path=docx_path,
        used_materials=[],
        used_model=used_model,
        structured_answers=structured_answers,
    )
