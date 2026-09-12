"""Camada de abstração multi-provider para resolução de atividades com IA.

Suporta três provedores BYOK configuráveis:
  - Google Gemini    (GEMINI_API_KEY)    → google-genai SDK
  - Anthropic Claude (ANTHROPIC_API_KEY) → anthropic SDK
  - DeepSeek         (DEEPSEEK_API_KEY)  → openai SDK com base_url customizada

O usuário pode escolher explicitamente o Provedor Principal (AI_PROVIDER)
e configurar uma Cadeia de Contingência / Fallback dinâmica entre os três provedores:
(AI_FALLBACK_PROVIDER_1, AI_FALLBACK_PROVIDER_2, AI_FALLBACK_PROVIDER_3).

Se o provedor principal sofrer timeout, estourar cotas (429) ou ficar indisponível,
o robô migra instantaneamente para o próximo provedor configurado na cadeia.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console

from config.settings import settings
from src.scraper.moodle_scraper import Assignment

console = Console()


def normalize_provider(name: Optional[str]) -> Optional[str]:
    """Normaliza o nome do provedor para um identificador canônico ou None."""
    if not name:
        return None
    cleaned = str(name).strip().lower()
    if cleaned in ("gemini", "google", "google-gemini", "google_gemini"):
        return "gemini"
    if cleaned in ("claude", "anthropic", "anthropic-claude", "anthropic_claude"):
        return "anthropic"
    if cleaned in ("deepseek", "deep-seek", "deep_seek"):
        return "deepseek"
    if cleaned in ("none", "nenhum", "desativado", "disabled", "off", "0", ""):
        return None
    return cleaned


def get_fallback_chain() -> List[str]:
    """Retorna a lista ordenada dos provedores configurados para execução e contingência."""
    primary = normalize_provider(getattr(settings, "AI_PROVIDER", "gemini")) or "gemini"

    raw_candidates = [
        primary,
        normalize_provider(getattr(settings, "AI_FALLBACK_PROVIDER_1", None)),
        normalize_provider(getattr(settings, "AI_FALLBACK_PROVIDER_2", None)),
        normalize_provider(getattr(settings, "AI_FALLBACK_PROVIDER_3", None)),
    ]

    chain: List[str] = []
    for c in raw_candidates:
        if c and c not in chain:
            chain.append(c)

    if not chain:
        chain = ["gemini"]

    return chain


def get_active_provider() -> str:
    """Retorna descrição clara do provedor principal e sua cadeia de contingência para o /status."""
    chain = get_fallback_chain()
    primary = chain[0] if chain else "gemini"

    def _format_prov(p: str) -> str:
        if p == "anthropic":
            model = settings.ANTHROPIC_MODEL or "claude-haiku-4-5"
            return f"Anthropic Claude ({model})"
        elif p == "deepseek":
            model = settings.DEEPSEEK_MODEL or "deepseek-chat"
            return f"DeepSeek ({model})"
        else:
            model = settings.GEMINI_MODEL or "gemini-3.5-flash"
            return f"Google Gemini ({model})"

    primary_str = _format_prov(primary)
    fallbacks = chain[1:]
    if fallbacks:
        fb_names = []
        for fb in fallbacks:
            if fb == "anthropic":
                fb_names.append("Claude")
            elif fb == "deepseek":
                fb_names.append("DeepSeek")
            else:
                fb_names.append("Gemini")
        return f"{primary_str} [Fallback: {' → '.join(fb_names)}]"
    return primary_str


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
# Backends de Execução (Claude e DeepSeek)
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

    api_key = settings.ANTHROPIC_API_KEY
    if not api_key or api_key in ("", "sua_chave_anthropic_aqui"):
        raise RuntimeError("Chave ANTHROPIC_API_KEY não configurada no .env.")

    model = settings.ANTHROPIC_MODEL or "claude-haiku-4-5"
    await _emit_log(on_log, f"Consultando Anthropic Claude ({model})...")

    client = anthropic.AsyncAnthropic(api_key=api_key)
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

    api_key = settings.DEEPSEEK_API_KEY
    if not api_key or api_key in ("", "sua_chave_deepseek_aqui"):
        raise RuntimeError("Chave DEEPSEEK_API_KEY não configurada no .env.")

    model = settings.DEEPSEEK_MODEL or "deepseek-chat"
    await _emit_log(on_log, f"Consultando DeepSeek ({model})...")

    client = AsyncOpenAI(
        api_key=api_key,
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
# AISolver — Fachada Unificada com Cadeia de Fallback Dinâmica
# ---------------------------------------------------------------------------

class AISolver:
    """Motor de resolução unificado com suporte flexível a Gemini, Claude e DeepSeek.

    Permite escolher o provedor principal e encadear múltiplos provedores
    de contingência em caso de erro, rate limit ou timeout.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.chain = get_fallback_chain()
        self.provider = self.chain[0] if self.chain else "gemini"
        self._gemini_solver = None

        # Sempre prepara GeminiSolver se estiver na cadeia ou como rede de segurança
        if "gemini" in self.chain or not self.chain:
            from src.solver.gemini_solver import GeminiSolver
            self._gemini_solver = GeminiSolver(api_key=api_key)

        console.print(
            f"[cyan]🤖 Provedor IA ativo: [bold]{get_active_provider()}[/bold][/cyan]"
        )

    def _is_provider_ready(self, provider: str) -> Tuple[bool, str]:
        """Verifica se as credenciais do provedor estão presentes."""
        if provider == "gemini":
            key = settings.GEMINI_API_KEY
            if not key or key in ("", "sua_chave_gemini_api_aqui"):
                return False, "GEMINI_API_KEY ausente ou não configurada no .env"
            return True, ""
        elif provider == "anthropic":
            key = settings.ANTHROPIC_API_KEY
            if not key or key in ("", "sua_chave_anthropic_aqui"):
                return False, "ANTHROPIC_API_KEY ausente ou não configurada no .env"
            return True, ""
        elif provider == "deepseek":
            key = settings.DEEPSEEK_API_KEY
            if not key or key in ("", "sua_chave_deepseek_aqui"):
                return False, "DEEPSEEK_API_KEY ausente ou não configurada no .env"
            return True, ""
        return False, f"Provedor desconhecido: {provider}"

    async def generate_text(
        self,
        system_instruction: str,
        user_message: str,
        temperature: float = 0.2,
        on_log: Optional[Any] = None,
    ) -> Tuple[str, str]:
        """Gera texto puro percorrendo a cadeia de provedores configurada."""
        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                console.print(f"[dim]Pulanado {prov}: {reason}[/dim]")
                continue

            try:
                if prov == "gemini":
                    if not self._gemini_solver:
                        from src.solver.gemini_solver import GeminiSolver
                        self._gemini_solver = GeminiSolver()
                    resp, used_model = await self._gemini_solver._generate_with_fallback(
                        contents=[user_message],
                        system_instruction=system_instruction,
                        temperature=temperature,
                        on_log=on_log,
                    )
                    text = resp.text if resp and resp.text else ""
                    return text, used_model

                elif prov == "anthropic":
                    return await _call_claude(system_instruction, user_message, on_log=on_log)

                elif prov == "deepseek":
                    return await _call_deepseek(system_instruction, user_message, on_log=on_log)

            except Exception as err:
                last_error = err
                console.print(f"[yellow]Aviso: Falha no provedor {prov}: {err}[/yellow]")
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha em {prov}. Acionando fallback: {next_prov}...")

        raise RuntimeError(
            f"Todos os provedores de IA da cadeia falharam ({' → '.join(self.chain)}). "
            f"Último erro: {last_error}"
        )

    async def solve_assignment(
        self,
        assignment: Assignment,
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None,
        on_log: Optional[Any] = None,
        auto_triggered: bool = False,
    ):
        """Gera resolução completa delegando através da cadeia de contingência configurada."""
        from src.solver.gemini_solver import extract_text_from_context_files

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

        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                console.print(f"[dim]Pulando {prov} na resolução: {reason}[/dim]")
                continue

            try:
                if prov == "gemini" and self._gemini_solver and self._gemini_solver.client:
                    # Executa via GeminiSolver nativo (com suporte à API de arquivos e seus próprios fallbacks)
                    return await self._gemini_solver.solve_assignment(
                        assignment=assignment,
                        user_notes=user_notes,
                        extra_context_files=extra_context_files,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                    )
                elif prov == "anthropic":
                    full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                    )
                elif prov == "deepseek":
                    full_text, used_model = await _call_deepseek(system_instruction, user_message, on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                    )
                elif prov == "gemini":
                    # Gemini sem cliente completo ou fallback geral
                    full_text, used_model = await self.generate_text(system_instruction, user_message, on_log=on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                    )

            except Exception as err:
                last_error = err
                console.print(f"[yellow]Aviso: Falha ao resolver com {prov}: {err}[/yellow]")
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha no provedor {prov}. Alternando para contingência: {next_prov}...")

        raise RuntimeError(
            f"Todos os provedores configurados falharam para esta atividade ({' → '.join(self.chain)}). "
            f"Último erro: {last_error}"
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
        """Resolve questionário ao vivo delegando através da cadeia de contingência."""
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
            "Você é um estudante universitário da UFMG realizando questionário no Moodle.\n"
            "Responda cada questão com letra e texto completo.\n"
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

        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                console.print(f"[dim]Pulando {prov} no questionário: {reason}[/dim]")
                continue

            try:
                if prov == "gemini" and self._gemini_solver and self._gemini_solver.client:
                    return await self._gemini_solver.solve_quiz_with_live_context(
                        assignment=assignment,
                        questions_data=questions_data,
                        user_notes=user_notes,
                        extra_context_files=extra_context_files,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                    )
                elif prov == "anthropic":
                    full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                        is_quiz=True,
                    )
                elif prov == "deepseek":
                    full_text, used_model = await _call_deepseek(system_instruction, user_message, on_log)
                    return await _build_solution_draft(
                        assignment=assignment,
                        full_text=full_text,
                        used_model=used_model,
                        on_log=on_log,
                        auto_triggered=auto_triggered,
                        is_quiz=True,
                    )

            except Exception as err:
                last_error = err
                console.print(f"[yellow]Aviso: Falha no quiz com {prov}: {err}[/yellow]")
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha no quiz via {prov}. Alternando para contingência: {next_prov}...")

        raise RuntimeError(
            f"Todos os provedores falharam para este questionário ({' → '.join(self.chain)}). "
            f"Último erro: {last_error}"
        )

    async def apply_revision(
        self,
        draft,
        revision_instructions: str,
        on_log: Optional[Any] = None,
    ):
        """Aplica revisão do usuário sobre um draft existente percorrendo a cadeia de contingência."""
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

        last_error = None

        for idx, prov in enumerate(self.chain):
            is_ready, reason = self._is_provider_ready(prov)
            if not is_ready:
                continue

            try:
                if prov == "gemini" and self._gemini_solver and self._gemini_solver.client:
                    return await self._gemini_solver.apply_revision(
                        draft=draft,
                        revision_instructions=revision_instructions,
                        on_log=on_log,
                    )
                elif prov == "anthropic":
                    full_text, used_model = await _call_claude(system_instruction, user_message, on_log)
                elif prov == "deepseek":
                    full_text, used_model = await _call_deepseek(system_instruction, user_message, on_log)
                else:
                    full_text, used_model = await self.generate_text(system_instruction, user_message, on_log=on_log)

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

            except Exception as err:
                last_error = err
                if idx < len(self.chain) - 1:
                    next_prov = self.chain[idx + 1]
                    await _emit_log(on_log, f"⚠️ Falha na revisão via {prov}. Alternando para: {next_prov}...")

        raise RuntimeError(f"Falha ao aplicar revisão em todos os provedores: {last_error}")


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
        # Resolução automática → gera PDF
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
