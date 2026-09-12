"""Módulo de resolução de atividades com IA utilizando o Google Gemini.

Contextualiza o problema com os materiais didáticos e slides da matéria
para gerar rascunhos rigorosos e fundamentados academicamente.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Silencia avisos informativos internos de AFC do SDK google-genai
logging.getLogger("google.genai.models").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

from google import genai
from google.genai import types
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel

from config.settings import settings
from src.scraper.moodle_scraper import Assignment, CourseMaterial, sanitize_filename
from src.solver.pdf_generator import AcademicPDFGenerator

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


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


def extract_text_from_context_files(files: List[Path]) -> str:
    """Extrai texto legível de PDFs, TXT, Markdown, CSV e JSON para injeção direta no prompt do Gemini."""
    extracted_blocks = []
    for file_path in files:
        if not file_path.exists() or file_path.stat().st_size == 0:
            continue
        ext = file_path.suffix.lower()
        content = ""
        try:
            if ext == ".pdf":
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(str(file_path))
                    pages_text = []
                    for p_idx, page in enumerate(reader.pages):
                        pt = page.extract_text()
                        if pt and pt.strip():
                            pages_text.append(f"[Página {p_idx+1}]\n{pt.strip()}")
                    if pages_text:
                        content = "\n\n".join(pages_text)
                except Exception as pdf_err:
                    console.print(f"  [yellow]Aviso ao extrair texto do PDF {file_path.name}: {pdf_err}[/yellow]")
            elif ext in [".txt", ".md", ".csv", ".json", ".xml", ".html"]:
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:
                    try:
                        content = file_path.read_text(encoding="latin-1", errors="replace").strip()
                    except Exception:
                        pass
        except Exception as read_err:
            console.print(f"  [yellow]Aviso ao ler arquivo de apoio {file_path.name}: {read_err}[/yellow]")

        if content:
            # Limita tamanho para evitar estourar tokens caso o material seja excessivamente extenso
            if len(content) > 35000:
                content = content[:35000] + "\n... [Texto truncado por limite de contexto]"
            extracted_blocks.append(
                f"--- INÍCIO DO ARQUIVO: {file_path.name} ---\n"
                f"{content}\n"
                f"--- FIM DO ARQUIVO: {file_path.name} ---"
            )

    if extracted_blocks:
        return "\n\n".join(extracted_blocks)
    return ""


class SolutionDraft(BaseModel):
    """Representa a resolução elaborada pela IA para a atividade."""
    assignment_id: str
    assignment_title: str
    course_name: str
    summary: str
    full_markdown: str
    output_path: Path
    pdf_path: Optional[Path] = None
    docx_path: Optional[Path] = None          # DOCX editável (resoluções manuais)
    used_materials: List[str] = []
    used_model: str = "gemini-3.8-flash"
    structured_answers: Optional[List[Dict[str, Any]]] = None
    auto_triggered: bool = False              # True = automático (daemon/emergência) → PDF
                                              # False = manual (/resolver)             → DOCX
    activity_type: str = "assign"             # "assign" ou "quiz"


class GeminiSolver:
    """Motor de resolução com Fallback Hierárquico e suporte a modelos inteligentes e ágeis."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        raw_hierarchy = [
            settings.GEMINI_MODEL,
            getattr(settings, "GEMINI_FALLBACK_MODEL_1", "gemini-3.8-flash"),
            getattr(settings, "GEMINI_FALLBACK_MODEL_2", "gemini-3.7-flash"),
            getattr(settings, "GEMINI_FALLBACK_MODEL_3", "gemini-3.5-flash-lite"),
        ]
        self.model_hierarchy = []
        for m in raw_hierarchy:
            if m and m not in self.model_hierarchy:
                self.model_hierarchy.append(m)

        self.timeout_seconds = getattr(settings, "GEMINI_TIMEOUT_SECONDS", 90)
        self.fallback_timeout_seconds = getattr(settings, "GEMINI_FALLBACK_TIMEOUT_SECONDS", 60)
        self.fallback_delay_seconds = getattr(settings, "GEMINI_FALLBACK_DELAY_SECONDS", 2.0)

        # Adiciona modelos rápidos de resguardo no fim da lista se ausentes
        for safe_model in ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"]:
            if safe_model not in self.model_hierarchy:
                self.model_hierarchy.append(safe_model)

        self.submissions_dir = settings.STORAGE_SUBMISSIONS_DIR
        self.materials_dir = settings.STORAGE_MATERIALS_DIR

        if not self.api_key or self.api_key == "sua_chave_gemini_api_aqui":
            console.print(
                "[bold red]Aviso: GEMINI_API_KEY não configurada no .env. "
                "Adquira sua chave gratuita em https://aistudio.google.com/[/bold red]"
            )
            self.client = None
        else:
            self.client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=int(self.timeout_seconds * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1)
                )
            )

    def _collect_context_files(self, assignment: Assignment) -> List[Path]:
        """Reúne exclusivamente os arquivos anexos diretos da tarefa baixados do Moodle.
        
        Materiais gerais da disciplina não são mais incluídos automaticamente para evitar
        sobrecarga, erros 503 e envio de conteúdo desnecessário. O usuário seleciona
        explicitamente os materiais desejados via Discord (/resolver e seletor).
        """
        collected: List[Path] = []

        # Anexos diretos da tarefa baixados da página do Moodle (enunciado, dados CSV, roteiros)
        for att in assignment.attachments:
            if att.local_path and att.local_path.exists() and att.local_path.stat().st_size > 0:
                # Limite de segurança de 15MB por anexo direto
                if att.local_path.stat().st_size <= 15 * 1024 * 1024:
                    collected.append(att.local_path)

        return collected

    async def _generate_with_fallback(
        self,
        contents: List[Any],
        system_instruction: Optional[str] = None,
        temperature: float = 0.1,
        on_log: Optional[Any] = None
    ) -> tuple[Any, str]:
        """Executa a geração de conteúdo percorrendo a hierarquia de modelos com timeouts e transição ágil."""
        if not self.client:
            raise RuntimeError("Chave GEMINI_API_KEY não informada. Configure a variável no arquivo .env.")

        last_error = None
        total_models = len(self.model_hierarchy)

        for idx, model_candidate in enumerate(self.model_hierarchy):
            is_primary = (idx == 0)
            timeout = self.timeout_seconds if is_primary else self.fallback_timeout_seconds
            role_label = "modelo principal" if is_primary else f"fallback {idx}"

            console.print(f"  [cyan]Tentando geração com {role_label}: [bold]{model_candidate}[/bold] (limite: {timeout}s)...[/cyan]")
            await _emit_log(on_log, f"Consultando {model_candidate} ({role_label})...")

            try:
                # Usa cliente assíncrono nativo com limite estrito de timeout por tentativa
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=model_candidate,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=temperature,
                            http_options=types.HttpOptions(
                                timeout=int(max(timeout, 1) * 1000),
                                retry_options=types.HttpRetryOptions(attempts=1)
                            )
                        )
                    ),
                    timeout=timeout
                )

                if response and response.text:
                    console.print(f"  [green]✔ Resposta gerada com sucesso via {model_candidate}![/green]")
                    await _emit_log(on_log, f"✔ Resolução concluída via {model_candidate}")
                    return response, model_candidate

            except asyncio.TimeoutError:
                last_error = TimeoutError(f"Tempo limite de {timeout}s esgotado em {model_candidate}")
                console.print(f"  [yellow]⏱ {model_candidate} excedeu o tempo limite ({timeout}s).[/yellow]")
                if idx < total_models - 1:
                    next_model = self.model_hierarchy[idx + 1]
                    await _emit_log(on_log, f"⏱ {model_candidate}: limite de {timeout}s esgotado. Alternando para {next_model}...")
                    if self.fallback_delay_seconds > 0:
                        await asyncio.sleep(self.fallback_delay_seconds)
                else:
                    await _emit_log(on_log, f"⏱ {model_candidate}: tempo limite esgotado.")

            except Exception as gen_err:
                last_error = gen_err
                err_str = str(gen_err)
                if "503" in err_str or "high demand" in err_str.lower() or "UNAVAILABLE" in err_str:
                    err_desc = "Servidores com alta demanda (503)"
                elif "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    err_desc = "Cota temporariamente excedida (429)"
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    err_desc = "Modelo descontinuado ou não encontrado (404)"
                else:
                    err_desc = err_str[:90]

                console.print(f"  [yellow]Aviso: Falha com {model_candidate} ({err_desc}).[/yellow]")
                if idx < total_models - 1:
                    next_model = self.model_hierarchy[idx + 1]
                    await _emit_log(on_log, f"⚠️ {model_candidate}: {err_desc}. Alternando para fallback {next_model}...")
                    if self.fallback_delay_seconds > 0:
                        await asyncio.sleep(self.fallback_delay_seconds)
                else:
                    await _emit_log(on_log, f"❌ {model_candidate}: {err_desc}.")

        raise RuntimeError(f"Todos os modelos da hierarquia falharam. Último erro: {last_error}")

    async def solve_assignment(
        self,
        assignment: Assignment,
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None,
        on_log: Optional[Any] = None,
        auto_triggered: bool = False,
    ) -> SolutionDraft:
        """Gera a resolução completa com fallback hierárquico.

        Args:
            auto_triggered: True → daemon/emergência → gera PDF.
                            False → /resolver manual → gera DOCX editável.
        """
        if not self.client:
            raise RuntimeError(
                "Chave GEMINI_API_KEY não informada. Configure a variável no arquivo .env."
            )

        console.print(
            f"[cyan]Iniciando resolução para: [bold]{assignment.title}[/bold] ({assignment.course_name})...[/cyan]"
        )
        await _emit_log(on_log, f"Iniciando resolução para '{assignment.title}'...")

        context_files = self._collect_context_files(assignment)
        if extra_context_files:
            for ef in extra_context_files:
                if ef.exists() and ef not in context_files:
                    context_files.append(ef)

        uploaded_gemini_files = []
        used_material_names = []

        try:
            # Faz upload dos arquivos de contexto diretamente para a Files API do Gemini
            for file_path in context_files:
                try:
                    console.print(f"  [dim]Carregando contexto: {file_path.name}...[/dim]")
                    await _emit_log(on_log, f"Carregando material de apoio: {file_path.name}")
                    if hasattr(self.client, "aio"):
                        uploaded = await asyncio.wait_for(
                            self.client.aio.files.upload(file=str(file_path)),
                            timeout=25
                        )
                    else:
                        uploaded = await asyncio.to_thread(self.client.files.upload, file=str(file_path))
                    uploaded_gemini_files.append(uploaded)
                    used_material_names.append(file_path.name)
                except Exception as up_err:
                    console.print(f"  [yellow]Falha ao carregar {file_path.name} para o Gemini: {up_err}[/yellow]")

            # Montagem do Prompt Acadêmico Rigoroso
            # Montagem do Prompt Humano Realista
            system_instruction = (
                "Você é um estudante universitário da UFMG realizando esta atividade acadêmica.\n"
                "Escreva a resolução EXATAMENTE como um aluno humano real entrega para o professor:\n\n"
                "DIRETRIZES OBRIGATÓRIAS:\n"
                "1. REGRA DE OURO - PRIORIDADE ABSOLUTA DO GABARITO / MATERIAL DE REFERÊNCIA:\n"
                "   - Se houver materiais de apoio, gabaritos ou anotações fornecidos contendo resoluções ou respostas para esta atividade, "
                "você DEVE seguir 100% as respostas, termos e sequências indicados neles.\n"
                "   - É ESTRITAMENTE PROIBIDO divergir, recalcular ou tentar 're-resolver' qualquer questão que já possua resposta explicitada no material de referência/gabarito.\n"
                "   - Mantenha com máxima fidelidade as sequências de Verdadeiro/Falso (ex: V-F-F-V) e listas de itens/associações (ex: C, B, D, A) dadas no gabarito.\n\n"
                "2. FOCO TOTAL NA ATIVIDADE ESPECÍFICA:\n"
                f"   - O título desta atividade é: '{assignment.title}'.\n"
                "   - Resolva EXCLUSIVAMENTE as questões pertencentes a esta atividade específica. "
                "Mesmo que os materiais de referência contenham gabaritos ou conteúdos de outras aulas, unidades ou módulos, "
                "NÃO responda nada além do que foi pedido para esta aula/atividade específica.\n\n"
                "3. PROIBIDO QUALQUER METATEXTO DE IA OU BOILERPLATE:\n"
                "   - NUNCA inclua 'Resumo Executivo', 'Relatório de Resolução', 'Introdução' ou conclusões genéricas.\n"
                "   - NUNCA mencione frases como 'todas as respostas foram rigorosamente extraídas do gabarito oficial', 'conforme o anexo', 'com base no material didático'. Escreva as respostas diretamente.\n"
                "   - NUNCA inclua seções vazias de 'Códigos e Scripts' se a matéria ou atividade não exigir programação.\n"
                "   - NUNCA invente seções de 'Referências Bibliográficas' a menos que solicitado expressamente no enunciado.\n\n"
                "4. FORMATO LIMPO E DIRETO:\n"
                "   - Comece diretamente com as questões:\n"
                "     ### Questão 1\n"
                "     [Sua resposta direta]\n\n"
                "     ### Questão 2\n"
                "     1. [Item 1]\n"
                "     2. [Item 2]\n\n"
                "5. DADOS ESTRUTURADOS PARA QUESTIONÁRIOS ONLINE (QUIZZES):\n"
                "   - Ao final da sua resposta, adicione um bloco de código oculto contendo as respostas mapeadas por questão:\n"
                "   ```json:answers\n"
                "   [\n"
                "     {\"question\": 1, \"answers\": [\"resposta 1\", \"resposta 2\"]},\n"
                "     {\"question\": 2, \"answers\": [\"resposta\"]}\n"
                "   ]\n"
                "   ```\n"
                "   (Esse bloco json será usado pelo robô para preencher o formulário no Moodle e será removido do PDF)."
            )

            prompt_content = [
                system_instruction,
                f"DISCIPLINA: {assignment.course_name}\n"
                f"ATIVIDADE ESPECÍFICA A RESOLVER: {assignment.title}\n"
                f"ENUNCIADO / INSTRUÇÕES:\n{assignment.description}\n"
            ]

            extracted_ref = extract_text_from_context_files(context_files)
            if extracted_ref:
                prompt_content.append(
                    "================================================================================\n"
                    "### MATERIAL DE REFERÊNCIA / GABARITO PRIORITÁRIO EXTRAÍDO DOS ARQUIVOS DE APOIO:\n"
                    f"{extracted_ref}\n"
                    "(ATENÇÃO: O material acima é a referência primária e oficial desta disciplina. Siga-o 100%!)\n"
                    "================================================================================\n"
                )

            if user_notes:
                prompt_content.append(
                    "================================================================================\n"
                    "### INSTRUÇÕES E ANOTAÇÕES PRIORITÁRIAS DO ALUNO:\n"
                    f"{user_notes}\n"
                    "(ATENÇÃO: Siga estritamente as respostas e orientações indicadas pelo aluno acima!)\n"
                    "================================================================================\n"
                )

            prompt_content.append("Por favor, resolva a atividade como o próprio aluno.")

            contents = prompt_content + uploaded_gemini_files

            # Geração com Fallback Hierárquico e Timeouts Rigorosos
            response, successful_model = await self._generate_with_fallback(
                contents=contents,
                system_instruction=system_instruction,
                temperature=0.2,
                on_log=on_log
            )

            full_text = response.text

            # Extrai dados estruturados para preenchimento de Quiz (se houver bloco json:answers)
            structured_answers = None
            json_match = re.search(r"```json:answers\s*\n(.*?)\n```", full_text, re.DOTALL)
            if json_match:
                try:
                    structured_answers = json.loads(json_match.group(1).strip())
                except Exception:
                    pass

            # Remove o bloco json:answers do Markdown para manter o PDF e visualização 100% humanos e limpos
            clean_markdown = re.sub(r"```json:answers\s*\n.*?\n```", "", full_text, flags=re.DOTALL).strip()

            # Resumo para o Discord: primeiros parágrafos limpos
            summary_lines = [l for l in clean_markdown.splitlines() if l.strip() and not l.startswith("#")]
            summary = "\n".join(summary_lines[:6]) if summary_lines else clean_markdown[:400]

            # Salva o arquivo de rascunho em disco
            safe_course = sanitize_filename(assignment.course_name)
            safe_title = sanitize_filename(assignment.title)
            dest_dir = self.submissions_dir / safe_course
            dest_dir.mkdir(parents=True, exist_ok=True)

            draft_path = dest_dir / f"{safe_title}_rascunho.md"
            draft_path.write_text(clean_markdown, encoding="utf-8")

            if auto_triggered:
                # Resolução automática (daemon/emergência) → PDF
                pdf_path = dest_dir / f"{safe_title}.pdf"
                try:
                    await _emit_log(on_log, "Compilando PDF acadêmico da resolução...")
                    pdf_gen = AcademicPDFGenerator()
                    await pdf_gen.render_pdf(
                        markdown_text=clean_markdown,
                        output_pdf_path=pdf_path,
                        course_name=assignment.course_name,
                        assignment_title=assignment.title
                    )
                    await _emit_log(on_log, f"✔ PDF acadêmico gerado: {pdf_path.name}")
                except Exception as pdf_err:
                    console.print(f"[yellow]Aviso ao gerar PDF: {pdf_err}[/yellow]")
                    pdf_path = None
                docx_path = None
            else:
                # Resolução manual → DOCX editável
                pdf_path = None
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
                except Exception as docx_err:
                    console.print(f"[yellow]Aviso ao gerar DOCX: {docx_err}[/yellow]")
                    docx_path = None

            console.print(
                Panel.fit(
                    f"[bold green]✔ Resolução elaborada com sucesso![/bold green]\n"
                    f"Markdown: [bold]{draft_path}[/bold]\n"
                    f"PDF: [bold]{pdf_path if pdf_path else 'Não gerado'}[/bold]\n"
                    f"Materiais utilizados: {', '.join(used_material_names) if used_material_names else 'Nenhum'}",
                    title="[bold green]Rascunho e PDF Concluídos[/bold green]",
                    border_style="green"
                )
            )

            return SolutionDraft(
                assignment_id=assignment.id,
                assignment_title=assignment.title,
                course_name=assignment.course_name,
                summary=summary,
                full_markdown=full_text,
                output_path=draft_path,
                pdf_path=pdf_path,
                docx_path=docx_path,
                used_materials=used_material_names,
                used_model=successful_model,
                structured_answers=structured_answers,
                auto_triggered=auto_triggered,
            )


        finally:
            # Limpeza dos arquivos temporários carregados na nuvem do Gemini
            for up in uploaded_gemini_files:
                try:
                    if self.client and hasattr(self.client, "aio"):
                        await self.client.aio.files.delete(name=up.name)
                    else:
                        await asyncio.to_thread(self.client.files.delete, name=up.name)
                except Exception:
                    pass

    async def solve_quiz_with_live_context(
        self,
        assignment: Assignment,
        questions_data: List[Dict[str, Any]],
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None,
        on_log: Optional[Any] = None,
        auto_triggered: bool = False,
    ) -> SolutionDraft:
        """Resolve o questionário utilizando o texto real e marcadores [[CAMPO_X]] extraídos ao vivo do Moodle."""

        if not self.client:
            raise RuntimeError("Chave GEMINI_API_KEY não informada. Configure a variável no arquivo .env.")

        console.print(
            f"[cyan]Resolvendo questionário com contexto ao vivo para: [bold]{assignment.title}[/bold]...[/cyan]"
        )
        await _emit_log(on_log, f"Iniciando resolução das questões de '{assignment.title}' com IA...")

        context_files = self._collect_context_files(assignment)
        if extra_context_files:
            for ef in extra_context_files:
                if ef.exists() and ef not in context_files:
                    context_files.append(ef)

        uploaded_gemini_files = []
        used_material_names = []

        try:
            for file_path in context_files:
                try:
                    console.print(f"  [dim]Carregando contexto: {file_path.name}...[/dim]")
                    await _emit_log(on_log, f"Carregando material de apoio: {file_path.name}")
                    if hasattr(self.client, "aio"):
                        uploaded = await asyncio.wait_for(
                            self.client.aio.files.upload(file=str(file_path)),
                            timeout=25
                        )
                    else:
                        uploaded = await asyncio.to_thread(self.client.files.upload, file=str(file_path))
                    uploaded_gemini_files.append(uploaded)
                    used_material_names.append(file_path.name)
                except Exception:
                    pass

            formatted_questions = []
            for q in questions_data:
                q_txt = q.get("fullTextWithTokens", "").strip()
                if q_txt:
                    if q.get("isInfoOnly"):
                        formatted_questions.append(f"[TEXTO DE CONTEXTO / LEITURA]\n{q_txt}")
                    else:
                        formatted_questions.append(f"### {q.get('qNumberText', 'Questão')}\n{q_txt}")

            questions_body = "\n\n".join(formatted_questions)

            system_instruction = (
                "Você é um estudante universitário da UFMG realizando uma atividade avaliativa no Moodle.\n"
                "Abaixo está o conteúdo extraído da tela do questionário, contendo questões avaliativas que podem conter:\n"
                "- Marcadores pontuais [[CAMPO_1]], [[CAMPO_2]]... que representam lacunas, caixas de texto, áreas de arrastar/soltar ou questões dissertativas;\n"
                "- Questões de múltipla escolha com alternativas (ex: a, b, c, d);\n"
                "- Questões de seleção múltipla (caixas de seleção / checkboxes) onde mais de uma opção pode estar correta;\n"
                "- Questões abertas/dissertativas que exigem redação de resposta fundamentada (ex: caixas de texto TinyMCE / Atto).\n\n"
                "DIRETRIZES DE RESOLUÇÃO:\n"
                "1. REGRA DE OURO - PRIORIDADE ABSOLUTA DO GABARITO / MATERIAL DE APOIO FORNECIDO:\n"
                "   - Se houver materiais de apoio, gabarito ou anotações fornecidos contendo respostas para esta atividade, você DEVE seguir 100% as respostas, termos e sequências indicados neles.\n"
                "   - É PROIBIDO DIVERGIR OU TENTAR 'RE-RESOLVER' UMA QUESTÃO QUE JÁ POSSUI RESPOSTA DADA NO MATERIAL DE APOIO OU GABARITO.\n"
                "   - Se o gabarito indica 'QUESTÃO 4: V-F-F-V', a sua resposta para a Questão 4 DEVE ser obrigatoriamente 'V-F-F-V' (jamais altere para V-F-F-F ou qualquer outra sequência).\n"
                "   - Se o gabarito indica para uma questão de correspondência/associação a sequência C, B, D, A, você DEVE manter exatamente a sequência de itens 1. C, 2. B, 3. D, 4. A.\n"
                "   - MAPEAMENTO DO EMBARALHAMENTO DO MOODLE: Frequentemente o Moodle embaralha as alternativas de uma questão. Você DEVE identificar no Moodle qual alternativa corresponde ao CONTEÚDO/TEXTO ou VALOR da resposta do gabarito (ex: se o gabarito indica 'Todas acima' e no Moodle 'Todas acima' aparece na letra 'd', aponte 'd. Todas acima').\n\n"
                "2. PREENCHA CADA CAMPO E QUESTÃO: Forneça a resposta para cada marcador [[CAMPO_X]], questão de múltipla escolha (Q1, Q2, etc.) e questão dissertativa.\n"
                "3. ALTERNATIVAS DE MÚLTIPLA ESCOLHA: Para garantir precisão caso o Moodle embaralhe a ordem das alternativas, sempre indique a letra E o texto completo da alternativa escolhida (ex: 'c. de instruções para o uso correto de algo').\n"
                "4. CAIXAS DE SELEÇÃO / CHECKBOXES: Se a questão permitir mais de uma alternativa correta, liste todas as letras e textos das alternativas corretas (ex: 'a. ..., c. ...').\n"
                "5. QUESTÕES DE CORRESPONDÊNCIA / LACUNAS: Se a questão possuir múltiplos sub-itens (ex: 1, 2, 3, 4), associe cada um rigorosamente conforme o gabarito e numere-os na folha de respostas (1. **item 1**, 2. **item 2**...).\n"
                "6. QUESTÕES DISSERTATIVAS / TEXTO ABERTO: Elabore respostas completas, acadêmicas e fundamentadas, mapeadas tanto para o respectivo [[CAMPO_X]] quanto para QX no JSON e na Folha de Respostas.\n"
                "7. ARRASTAR E SOLTAR (DRAG & DROP): Preencha cada [[CAMPO_X]] com o texto exato da palavra a ser arrastada para aquela posição conforme o gabarito/enunciado.\n"
                "8. COERÊNCIA GRAMATICAL: Respeite a concordância gramatical, sintaxe e tempo verbal.\n\n"
                "FORMATO OBRIGATÓRIO DE SAÍDA:\n"
                "Sua resposta deve conter DUAS PARTES:\n\n"
                "PARTE 1: Bloco JSON estruturado (no início, usado pelo robô para preenchimento automático no Moodle):\n"
                "```json:answers\n"
                "{\n"
                '  "CAMPO_1": "resposta da lacuna 1 ou palavra arrastada",\n'
                '  "Q1": "letra e texto completo da alternativa escolhida (ex: c. de instruções para o uso correto de algo)",\n'
                '  "Q2": "texto da resposta dissertativa elaborada ou alternativas"\n'
                "}\n"
                "```\n\n"
                "PARTE 2: Folha de Respostas Acadêmica (renderizada no PDF do estudante):\n"
                "Logo abaixo do bloco JSON, escreva uma folha de respostas limpa, elegante e organizada para leitura:\n"
                "- Separe estritamente por questão avaliativa (ex: '### Questão 1', '### Questão 2').\n"
                "- Para questões com lacunas ou listas de palavras, liste as respostas de forma limpa e numerada:\n"
                "  1. **palavra 1**\n"
                "  2. **palavra 2**\n"
                "- Para questões discursivas ou de múltipla escolha:\n"
                "  - **Resposta:** [letra e texto completo da alternativa, ou texto completo da resposta dissertativa]\n"
                "- PROIBIÇÃO ESTRITA DE RUÍDOS DE TELA:\n"
                "  * NUNCA inclua seções como '### Informação' ou blocos de texto introdutórios.\n"
                "  * NUNCA reproduza lixo do Moodle como 'Texto da questão', 'Texto informativo', 'Resposta 1 Questão 1', 'Verificar Questão', 'Feedback' ou botões.\n"
                "  * O PDF final deve conter exclusivamente a resolução elegante das questões, pronta para entrega acadêmica."
            )

            prompt_content = [
                system_instruction,
                f"DISCIPLINA: {assignment.course_name}\n"
                f"ATIVIDADE: {assignment.title}\n\n"
                f"TEXTO REAL EXTRAÍDO DO QUESTIONÁRIO NO MOODLE:\n{questions_body}\n"
            ]

            extracted_ref = extract_text_from_context_files(context_files)
            if extracted_ref:
                prompt_content.append(
                    "================================================================================\n"
                    "### MATERIAL DE REFERÊNCIA / GABARITO PRIORITÁRIO EXTRAÍDO DOS ARQUIVOS DE APOIO:\n"
                    f"{extracted_ref}\n"
                    "(ATENÇÃO: O material acima contém as respostas e referências oficiais desta disciplina. Siga-o 100%!)\n"
                    "================================================================================\n"
                )

            if user_notes:
                prompt_content.append(
                    "================================================================================\n"
                    "### ANOTAÇÕES / GABARITO FORNECIDO PELO ALUNO:\n"
                    f"{user_notes}\n"
                    "(ATENÇÃO: Se as anotações do aluno contiverem respostas ou instruções específicas, siga-as rigorosamente!)\n"
                    "================================================================================\n"
                )

            contents = prompt_content + uploaded_gemini_files

            # Geração com Fallback Hierárquico e Timeouts Rigorosos
            response, successful_model = await self._generate_with_fallback(
                contents=contents,
                system_instruction=system_instruction,
                temperature=0.1,
                on_log=on_log
            )

            full_text = response.text

            structured_dict = {}
            json_match = re.search(r"```(?:json:answers|json)\s*\n(.*?)\n```", full_text, re.DOTALL)
            if json_match:
                try:
                    structured_dict = json.loads(json_match.group(1).strip())
                except Exception:
                    pass

            clean_markdown = re.sub(r"```(?:json:answers|json)\s*\n.*?\n```", "", full_text, flags=re.DOTALL).strip()

            # Pós-processamento de limpeza cirúrgica de resíduos do Moodle
            clean_markdown = re.sub(r"(?i)texto (?:informativo|da questão)", "", clean_markdown)
            clean_markdown = re.sub(r"(?i)resposta \d+\s*questão \d+", "", clean_markdown)
            clean_markdown = re.sub(r"(?i)verificar questão \d+", "", clean_markdown)
            clean_markdown = re.sub(r"### Informação\s*\n.*?(?=### Questão|\Z)", "", clean_markdown, flags=re.DOTALL)
            clean_markdown = re.sub(r"\n{3,}", "\n\n", clean_markdown).strip()

            # Fallback inteligente: se o bloco JSON estiver ausente ou incompleto, extrai do Markdown gerado
            if not isinstance(structured_dict, dict):
                structured_dict = {}

            q_matches = list(re.finditer(r"###\s*(?:Quest[ãa]o|Q)\s*(\d+)\s*\n+(.*?)(?=\n###|\Z)", clean_markdown, re.DOTALL | re.IGNORECASE))
            for m in q_matches:
                q_num = m.group(1)
                q_body = m.group(2).strip()
                ans_key = f"Q{q_num}"
                if ans_key not in structured_dict:
                    # Captura "- **Resposta:** c. ..." ou "**Resposta:** c. ..." (inclusive múltiplas linhas)
                    resp_m = re.search(r"\*\*(?:Resposta|Alternativa):\*\*\s*([\s\S]+?)(?=\n\*\*(?:Explicação|Justificativa):|\n###|\Z)", q_body, re.IGNORECASE)
                    if resp_m and resp_m.group(1).strip():
                        structured_dict[ans_key] = resp_m.group(1).strip()
                    else:
                        # Captura listas numeradas 1. **palavra**
                        items = re.findall(r"^\s*\d+\.\s*\*{0,2}(.*?)\*{0,2}\s*$", q_body, re.MULTILINE)
                        if items:
                            clean_items = []
                            for idx_sub, sub_val in enumerate(items, 1):
                                clean_val = sub_val.strip("* ").strip()
                                if clean_val:
                                    clean_items.append(clean_val)
                                    structured_dict[f"Q{q_num}_{idx_sub}"] = clean_val
                                    if any(sep in clean_val for sep in ["→", "->", ":"]):
                                        parts = re.split(r"[→\->:]", clean_val, maxsplit=1)
                                        if len(parts) == 2:
                                            k_label = parts[0].strip("* ").strip()
                                            v_target = parts[1].strip("* ").strip()
                                            if k_label and v_target:
                                                structured_dict[f"Q{q_num}_{k_label}"] = v_target
                                                structured_dict[k_label] = v_target
                            if clean_items and ans_key not in structured_dict:
                                structured_dict[ans_key] = ", ".join(clean_items)
                        else:
                            # Resposta dissertativa / texto aberto sem marcador
                            clean_body = re.sub(r"^(?:Texto da questão|Enunciado:?|Pergunta:?)\s*", "", q_body, flags=re.IGNORECASE).strip()
                            if clean_body:
                                structured_dict[ans_key] = clean_body

            summary_lines = [l for l in clean_markdown.splitlines() if l.strip() and not l.startswith("#")]
            summary = "\n".join(summary_lines[:8]) if summary_lines else clean_markdown[:400]

            safe_course = sanitize_filename(assignment.course_name)
            safe_title = sanitize_filename(assignment.title)
            dest_dir = self.submissions_dir / safe_course
            dest_dir.mkdir(parents=True, exist_ok=True)

            draft_path = dest_dir / f"{safe_title}_rascunho.md"
            draft_path.write_text(clean_markdown, encoding="utf-8")

            if auto_triggered:
                # Quiz automático → PDF
                pdf_path = dest_dir / f"{safe_title}.pdf"
                try:
                    await _emit_log(on_log, "Compilando folha de respostas em PDF...")
                    pdf_gen = AcademicPDFGenerator()
                    await pdf_gen.render_pdf(
                        markdown_text=clean_markdown,
                        output_pdf_path=pdf_path,
                        course_name=assignment.course_name,
                        assignment_title=assignment.title
                    )
                    await _emit_log(on_log, f"✔ Folha de respostas em PDF gerada: {pdf_path.name}")
                except Exception as pdf_err:
                    console.print(f"[yellow]Aviso ao gerar PDF do quiz: {pdf_err}[/yellow]")
                    pdf_path = None
                docx_path = None
            else:
                # Quiz manual → DOCX editável
                pdf_path = None
                docx_path = dest_dir / f"{safe_title}.docx"
                try:
                    await _emit_log(on_log, "Gerando folha de respostas em DOCX editável...")
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
                except Exception as docx_err:
                    console.print(f"[yellow]Aviso ao gerar DOCX do quiz: {docx_err}[/yellow]")
                    docx_path = None

            return SolutionDraft(
                assignment_id=assignment.id,
                assignment_title=assignment.title,
                course_name=assignment.course_name,
                summary=summary,
                full_markdown=clean_markdown,
                output_path=draft_path,
                pdf_path=pdf_path,
                docx_path=docx_path,
                used_materials=used_material_names,
                used_model=successful_model,
                structured_answers=[{"key": k, "value": v} for k, v in structured_dict.items()] if structured_dict else None,
                auto_triggered=auto_triggered,
            )


        finally:
            for up in uploaded_gemini_files:
                try:
                    if self.client and hasattr(self.client, "aio"):
                        await self.client.aio.files.delete(name=up.name)
                    else:
                        await asyncio.to_thread(self.client.files.delete, name=up.name)
                except Exception:
                    pass


    async def apply_revision(
        self,
        draft: "SolutionDraft",
        revision_instructions: str,
        on_log: Optional[Any] = None,
    ) -> "SolutionDraft":
        """Re-gera o DOCX aplicando instruções de modificação do usuário.

        Fluxo:
          1. Lê o markdown atual do draft
          2. Envia para a IA com as instruções de modificação
          3. Gera novo DOCX e retorna SolutionDraft atualizado
        """
        if not self.client:
            raise RuntimeError("Chave GEMINI_API_KEY não informada.")

        await _emit_log(on_log, f"Aplicando modificações solicitadas pelo aluno...")

        system_instruction = (
            "Você é um assistente acadêmico. O aluno revisou a resolução abaixo e quer que você aplique "
            "as modificações indicadas. Mantenha o formato Markdown com ### Questão X. "
            "Aplique APENAS as mudanças pedidas sem alterar o restante."
        )
        user_message = (
            f"RESOLUÇÃO ATUAL:\n{draft.full_markdown}\n\n"
            f"INSTRUÇÕES DE MODIFICAÇÃO DO ALUNO:\n{revision_instructions}\n\n"
            "Gere a resolução modificada completa."
        )

        contents = [system_instruction, user_message]
        response, used_model = await self._generate_with_fallback(
            contents=contents,
            system_instruction=system_instruction,
            temperature=0.1,
            on_log=on_log,
        )

        # Extrai respostas estruturadas se for questionário
        json_match = re.search(r"```(?:json:answers|json)\s*\n(.*?)\n```", response.text, re.DOTALL)
        structured_answers = draft.structured_answers
        if json_match:
            try:
                import json as _json
                parsed = _json.loads(json_match.group(1).strip())
                if isinstance(parsed, list):
                    structured_answers = parsed
                elif isinstance(parsed, dict):
                    structured_answers = [{"key": k, "value": v} for k, v in parsed.items()]
            except Exception:
                pass

        new_markdown = re.sub(
            r"```(?:json:answers|json)\s*\n.*?\n```", "", response.text, flags=re.DOTALL
        ).strip()

        safe_course = sanitize_filename(draft.course_name)
        safe_title = sanitize_filename(draft.assignment_title)
        dest_dir = self.submissions_dir / safe_course

        draft_path = dest_dir / f"{safe_title}_rascunho_v2.md"
        draft_path.write_text(new_markdown, encoding="utf-8")

        # Nova revisão sempre é manual → DOCX
        docx_path = dest_dir / f"{safe_title}_revisado.docx"
        try:
            from src.solver.docx_generator import AcademicDocxGenerator
            ok = AcademicDocxGenerator().generate_docx(
                markdown_text=new_markdown,
                output_path=docx_path,
                course_name=draft.course_name,
                assignment_title=draft.assignment_title,
            )
            if not ok:
                docx_path = None
        except Exception as e:
            console.print(f"[yellow]Aviso ao gerar DOCX revisado: {e}[/yellow]")
            docx_path = None

        return SolutionDraft(
            assignment_id=draft.assignment_id,
            assignment_title=draft.assignment_title,
            course_name=draft.course_name,
            summary=new_markdown[:300],
            full_markdown=new_markdown,
            output_path=draft_path,
            pdf_path=None,
            docx_path=docx_path,
            used_materials=draft.used_materials,
            used_model=used_model,
            auto_triggered=False,
            structured_answers=structured_answers,
            activity_type=getattr(draft, "activity_type", "assign"),
        )


async def main():
    """CLI para teste de resolução direta de uma tarefa."""
    parser = argparse.ArgumentParser(description="Moodle AI Solver (Google Gemini)")
    parser.add_argument("--test-stat", action="store_true", help="Gera resolução de teste para o Exercício 1 de Estatística")
    args = parser.parse_args()

    solver = GeminiSolver()
    if not solver.client:
        console.print("[red]Erro: Configure GEMINI_API_KEY no .env para testar o gerador.[/red]")
        sys.exit(1)

    if args.test_stat:
        # Mock com os dados reais já coletados de Exercício 1 de Estatística
        assign = Assignment(
            id="102068",
            course_id="8206",
            course_name="2026_2 - FUNDAMENTOS DE ESTATÍSTICA E CIÊNCIA DE DADOS - TB1",
            title="Exercício 1 - Estatística Descritiva",
            url="https://virtual.ufmg.br/20262/mod/assign/view.php?id=102068",
            description=(
                "O trabalho deve ser feito de preferência no R em forma de relatório.\n"
                "Postar até 11:00 do dia 20/08.\n"
                "Anexos: E1-Trabalho Descritiva.pdf e Maratona.csv"
            ),
            attachments=[
                CourseMaterial(
                    id="e1_pdf",
                    course_id="8206",
                    title="E1-Trabalho Descritiva",
                    url="https://virtual.ufmg.br/20262/mod_assign/...",
                    filename="E1-Trabalho Descritiva.pdf",
                    local_path=settings.STORAGE_MATERIALS_DIR / "2026_2 - FUNDAMENTOS DE ESTATÍSTICA E CIÊNCIA DE DADOS - TB1" / "assignments" / "Exercício 1 - Estatística Descritiva" / "E1-Trabalho Descritiva.pdf"
                )
            ]
        )

        draft = await solver.solve_assignment(assign)
        console.print(f"\n[cyan]Resumo Gerado:[/cyan]\n{draft.summary}\n")


if __name__ == "__main__":
    asyncio.run(main())
