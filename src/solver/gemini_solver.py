"""Módulo de resolução de atividades com IA utilizando o Google Gemini.

Contextualiza o problema com os materiais didáticos e slides da matéria
para gerar rascunhos rigorosos e fundamentados academicamente.
"""

import argparse
import asyncio
import os
import re
import sys
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

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


class SolutionDraft(BaseModel):
    """Representa a resolução elaborada pela IA para a atividade."""
    assignment_id: str
    assignment_title: str
    course_name: str
    summary: str
    full_markdown: str
    output_path: Path
    pdf_path: Optional[Path] = None
    used_materials: List[str] = []
    used_model: str = "gemini-3.8-flash"
    structured_answers: Optional[List[Dict[str, Any]]] = None


class GeminiSolver:
    """Motor de resolução com Fallback Hierárquico: 3.8-flash -> 3.7-flash -> 3.5-flash-lite."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model_hierarchy = [
            settings.GEMINI_MODEL,
            settings.GEMINI_FALLBACK_MODEL_1,
            settings.GEMINI_FALLBACK_MODEL_2,
        ]
        self.submissions_dir = settings.STORAGE_SUBMISSIONS_DIR
        self.materials_dir = settings.STORAGE_MATERIALS_DIR

        if not self.api_key or self.api_key == "sua_chave_gemini_api_aqui":
            console.print(
                "[bold red]Aviso: GEMINI_API_KEY não configurada no .env. "
                "Adquira sua chave gratuita em https://aistudio.google.com/[/bold red]"
            )
            self.client = None
        else:
            self.client = genai.Client(api_key=self.api_key)

    def _collect_context_files(self, assignment: Assignment) -> List[Path]:
        """Reúne todos os arquivos pertinentes à tarefa e à disciplina."""
        collected: List[Path] = []
        safe_course = sanitize_filename(assignment.course_name)
        course_path = self.materials_dir / safe_course

        # 1. Anexos diretos da tarefa (enunciado, dados CSV, roteiros)
        for att in assignment.attachments:
            if att.local_path and att.local_path.exists() and att.local_path.stat().st_size > 0:
                collected.append(att.local_path)

        # 2. Materiais gerais da disciplina (slides, apostilas, listas)
        if course_path.exists():
            for p in course_path.iterdir():
                if p.is_file() and p.suffix.lower() in [".pdf", ".csv", ".txt", ".docx"]:
                    # Não re-adiciona se já estiver na lista
                    if p not in collected:
                        collected.append(p)

        return collected

    async def solve_assignment(
        self,
        assignment: Assignment,
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None
    ) -> SolutionDraft:
        """Gera a resolução completa com fallback hierárquico (3.8-flash -> 3.7-flash -> 3.5-flash-lite)."""
        if not self.client:
            raise RuntimeError(
                "Chave GEMINI_API_KEY não informada. Configure a variável no arquivo .env."
            )

        console.print(
            f"[cyan]Iniciando resolução para: [bold]{assignment.title}[/bold] ({assignment.course_name})...[/cyan]"
        )

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
                    uploaded = self.client.files.upload(file=str(file_path))
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
                "1. FOCO TOTAL NA ATIVIDADE ESPECÍFICA:\n"
                f"   - O título desta atividade é: '{assignment.title}'.\n"
                "   - Resolva EXCLUSIVAMENTE as questões pertencentes a esta atividade específica. "
                "Mesmo que os materiais de referência contenham gabaritos ou conteúdos de outras aulas, unidades ou módulos, "
                "NÃO responda nada além do que foi pedido para esta aula/atividade específica.\n\n"
                "2. PROIBIDO QUALQUER METATEXTO DE IA OU BOILERPLATE:\n"
                "   - NUNCA inclua 'Resumo Executivo', 'Relatório de Resolução', 'Introdução' ou conclusões genéricas.\n"
                "   - NUNCA mencione gabaritos, arquivos anexos ou referências externas (ex: proibições estritas de frases como "
                "'todas as respostas foram rigorosamente extraídas do gabarito oficial', 'conforme o anexo', 'com base no material didático').\n"
                "   - NUNCA inclua seções vazias de 'Códigos e Scripts' se a matéria ou atividade não exigir programação.\n"
                "   - NUNCA invente seções de 'Referências Bibliográficas' a menos que solicitado expressamente no enunciado.\n\n"
                "3. FORMATO LIMPO E DIRETO:\n"
                "   - Comece diretamente com as questões:\n"
                "     ### Questão 1\n"
                "     [Sua resposta direta]\n\n"
                "     ### Questão 2\n"
                "     1. [Item 1]\n"
                "     2. [Item 2]\n\n"
                "4. DADOS ESTRUTURADOS PARA QUESTIONÁRIOS ONLINE (QUIZZES):\n"
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

            if user_notes:
                prompt_content.append(f"INSTRUÇÕES ADICIONAIS DO ALUNO:\n{user_notes}\n")

            prompt_content.append("Por favor, resolva a atividade como o próprio aluno.")

            contents = prompt_content + uploaded_gemini_files

            # Loop de Fallback Hierárquico: 3.8-flash -> 3.7-flash -> 3.5-flash-lite
            response = None
            successful_model = self.model_hierarchy[0]
            last_error = None

            for model_candidate in self.model_hierarchy:
                try:
                    console.print(f"  [cyan]Tentando geração com: [bold]{model_candidate}[/bold]...[/cyan]")
                    response = self.client.models.generate_content(
                        model=model_candidate,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            temperature=0.2,
                        )
                    )
                    if response and response.text:
                        successful_model = model_candidate
                        console.print(f"  [green]✔ Resolução concluída com sucesso via {model_candidate}![/green]")
                        break
                except Exception as gen_err:
                    console.print(f"  [yellow]Aviso: Falha com {model_candidate} ({gen_err}). Acionando próximo modelo...[/yellow]")
                    last_error = gen_err

            if not response or not response.text:
                raise RuntimeError(f"Todos os modelos da hierarquia falharam. Último erro: {last_error}")

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

            # Renderiza o documento em PDF limpo
            pdf_path = dest_dir / f"{safe_title}.pdf"
            try:
                pdf_gen = AcademicPDFGenerator()
                await pdf_gen.render_pdf(
                    markdown_text=clean_markdown,
                    output_pdf_path=pdf_path,
                    course_name=assignment.course_name,
                    assignment_title=assignment.title
                )
            except Exception as pdf_err:
                console.print(f"[yellow]Aviso ao gerar PDF: {pdf_err}[/yellow]")
                pdf_path = None

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
                used_materials=used_material_names,
                used_model=successful_model,
                structured_answers=structured_answers
            )

        finally:
            # Limpeza dos arquivos temporários carregados na nuvem do Gemini
            for up in uploaded_gemini_files:
                try:
                    self.client.files.delete(name=up.name)
                except Exception:
                    pass

    async def solve_quiz_with_live_context(
        self,
        assignment: Assignment,
        questions_data: List[Dict[str, Any]],
        user_notes: Optional[str] = None,
        extra_context_files: Optional[List[Path]] = None
    ) -> SolutionDraft:
        """Resolve o questionário utilizando o texto real e marcadores [[CAMPO_X]] extraídos ao vivo do Moodle."""
        if not self.client:
            raise RuntimeError("Chave GEMINI_API_KEY não informada. Configure a variável no arquivo .env.")

        console.print(
            f"[cyan]Resolvendo questionário com contexto ao vivo para: [bold]{assignment.title}[/bold]...[/cyan]"
        )

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
                    uploaded = self.client.files.upload(file=str(file_path))
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
                "- Marcadores pontuais [[CAMPO_1]], [[CAMPO_2]]... que representam lacunas ou caixas de texto a serem preenchidas;\n"
                "- Questões de múltipla escolha com alternativas (ex: a, b, c, d).\n\n"
                "DIRETRIZES DE RESOLUÇÃO:\n"
                "1. PREENCHA CADA CAMPO E QUESTÃO: Forneça a resposta para cada marcador [[CAMPO_X]] e para cada questão de múltipla escolha (Q1, Q2, etc.).\n"
                "2. ALTERNATIVAS DE MÚLTIPLA ESCOLHA: Para garantir precisão caso o Moodle embaralhe a ordem das alternativas, sempre indique a letra E o texto completo da alternativa escolhida (ex: 'c. de instruções para o uso correto de algo').\n"
                "3. ADEQUAÇÃO AO CONTEXTO: Responda com a máxima precisão e coerência conforme o enunciado e as regras da matéria.\n"
                "4. COERÊNCIA GRAMATICAL: Respeite a concordância gramatical, sintaxe e tempo verbal.\n\n"
                "FORMATO OBRIGATÓRIO DE SAÍDA:\n"
                "Sua resposta deve conter DUAS PARTES:\n\n"
                "PARTE 1: Bloco JSON estruturado (no início, usado pelo robô para preenchimento automático no Moodle):\n"
                "```json:answers\n"
                "{\n"
                '  "CAMPO_1": "resposta da lacuna 1",\n'
                '  "Q1": "letra e texto completo da alternativa escolhida (ex: c. de instruções para o uso correto de algo)",\n'
                '  "Q2": "letra e texto completo da alternativa escolhida (ex: b. a pessoa utilizando o produto)"\n'
                "}\n"
                "```\n\n"
                "PARTE 2: Folha de Respostas Acadêmica (renderizada no PDF do estudante):\n"
                "Logo abaixo do bloco JSON, escreva uma folha de respostas limpa, elegante e organizada para leitura:\n"
                "- Separe estritamente por questão avaliativa (ex: '### Questão 1', '### Questão 2').\n"
                "- Para questões com lacunas ou listas de palavras, liste as respostas de forma limpa e numerada:\n"
                "  1. **palavra 1**\n"
                "  2. **palavra 2**\n"
                "- Para questões discursivas ou de múltipla escolha:\n"
                "  - **Resposta:** [letra e texto completo da alternativa]\n"
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

            if user_notes:
                prompt_content.append(f"OBSERVAÇÕES DO ALUNO:\n{user_notes}\n")

            contents = prompt_content + uploaded_gemini_files

            response = None
            successful_model = self.model_hierarchy[0]
            for model_candidate in self.model_hierarchy:
                try:
                    console.print(f"  [cyan]Tentando geração com: [bold]{model_candidate}[/bold]...[/cyan]")
                    response = self.client.models.generate_content(
                        model=model_candidate,
                        contents=contents,
                        config=types.GenerateContentConfig(temperature=0.1)
                    )
                    if response and response.text:
                        successful_model = model_candidate
                        console.print(f"  [green]✔ Resolução ao vivo concluída com sucesso via {model_candidate}![/green]")
                        break
                except Exception as gen_err:
                    console.print(f"  [yellow]Aviso: Falha com {model_candidate} ({gen_err}). Acionando próximo modelo...[/yellow]")

            if not response or not response.text:
                raise RuntimeError("Falha ao gerar respostas com IA para o questionário.")

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
                    # Captura "- **Resposta:** c. ..." ou "**Resposta:** c. ..."
                    resp_m = re.search(r"\*\*(?:Resposta|Alternativa):\*\*\s*(.+)", q_body, re.IGNORECASE)
                    if resp_m:
                        structured_dict[ans_key] = resp_m.group(1).strip()
                    else:
                        # Captura listas numeradas 1. **palavra**
                        items = re.findall(r"^\s*\d+\.\s*\*{0,2}(.*?)\*{0,2}\s*$", q_body, re.MULTILINE)
                        if items:
                            for idx_sub, sub_val in enumerate(items, 1):
                                clean_val = sub_val.strip("* ").strip()
                                if clean_val:
                                    structured_dict[f"Q{q_num}_{idx_sub}"] = clean_val

            summary_lines = [l for l in clean_markdown.splitlines() if l.strip() and not l.startswith("#")]
            summary = "\n".join(summary_lines[:8]) if summary_lines else clean_markdown[:400]

            safe_course = sanitize_filename(assignment.course_name)
            safe_title = sanitize_filename(assignment.title)
            dest_dir = self.submissions_dir / safe_course
            dest_dir.mkdir(parents=True, exist_ok=True)

            draft_path = dest_dir / f"{safe_title}_rascunho.md"
            draft_path.write_text(clean_markdown, encoding="utf-8")

            pdf_path = dest_dir / f"{safe_title}.pdf"
            try:
                pdf_gen = AcademicPDFGenerator()
                await pdf_gen.render_pdf(
                    markdown_text=clean_markdown,
                    output_pdf_path=pdf_path,
                    course_name=assignment.course_name,
                    assignment_title=assignment.title
                )
            except Exception as pdf_err:
                pdf_path = None

            return SolutionDraft(
                assignment_id=assignment.id,
                assignment_title=assignment.title,
                course_name=assignment.course_name,
                summary=summary,
                full_markdown=clean_markdown,
                output_path=draft_path,
                pdf_path=pdf_path,
                used_materials=used_material_names,
                used_model=successful_model,
                structured_answers=[{"key": k, "value": v} for k, v in structured_dict.items()] if structured_dict else None
            )

        finally:
            for up in uploaded_gemini_files:
                try:
                    self.client.files.delete(name=up.name)
                except Exception:
                    pass


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
