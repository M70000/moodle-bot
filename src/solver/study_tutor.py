"""Módulo de Estudo Ativo, Tutor Acadêmico e Simulado Interativo.

Utiliza a base de materiais e slides das disciplinas (storage/materials/)
para tirar dúvidas conceituais, criar baralhos para o Anki e gerar simulados interativos.
"""

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console

from config.settings import settings
from src.scraper.moodle_scraper import sanitize_filename
from src.solver.gemini_solver import GeminiSolver, extract_text_from_context_files

console = Console()
logger = logging.getLogger("moodle_bot.study_tutor")


def normalize_str(text: str) -> str:
    """Normaliza texto removendo acentos e convertendo para minúsculas."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join([c for c in nfkd if not unicodedata.combining(c)]).lower().strip()


def resolve_course_materials_dir(discipline: str) -> Optional[Path]:
    """Localiza o diretório correspondente da disciplina em storage/materials/."""
    mat_dir = settings.STORAGE_MATERIALS_DIR
    if not mat_dir.exists():
        return None

    # 1. Busca exata por nome de pasta
    exact_dir = mat_dir / sanitize_filename(discipline)
    if exact_dir.exists() and exact_dir.is_dir():
        return exact_dir

    # 2. Busca com normalização (sem acentos e case insensitive)
    norm_target = normalize_str(discipline)
    best_match = None
    for p in mat_dir.iterdir():
        if p.is_dir() and not p.name.startswith("."):
            norm_name = normalize_str(p.name)
            if norm_target and (norm_target == norm_name or norm_target in norm_name or norm_name in norm_target):
                return p
            # Match parcial por tokens
            tokens = [t for t in norm_target.split() if len(t) > 2]
            if tokens and all(t in norm_name for t in tokens):
                best_match = p

    return best_match


def get_course_study_materials(discipline: str, specific_material: Optional[str] = None) -> List[Path]:
    """Coleta arquivos didáticos válidos da disciplina para alimentar o contexto do Gemini."""
    target_dir = resolve_course_materials_dir(discipline)
    if not target_dir or not target_dir.is_dir():
        return []

    valid_exts = {".pdf", ".csv", ".docx", ".txt", ".md", ".json"}
    all_files = [
        f for f in target_dir.iterdir()
        if f.is_file() and f.suffix.lower() in valid_exts and f.stat().st_size > 0
    ]

    if specific_material:
        norm_spec = normalize_str(specific_material)
        matched = [f for f in all_files if norm_spec in normalize_str(f.name)]
        if matched:
            return matched

    # Ordena por tamanho/nome e seleciona até 5 arquivos para caber no contexto com folga
    all_files.sort(key=lambda x: x.name)
    return all_files[:6]


def clean_json_text(text: str) -> str:
    """Extrai bloco JSON limpo de respostas que possam conter markdown ou texto auxiliar."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    # Se começar com [ ou {, busca até o último correspondente
    start_bracket = text.find("[")
    end_bracket = text.rfind("]")
    if start_bracket != -1 and end_bracket > start_bracket:
        return text[start_bracket : end_bracket + 1].strip()

    start_brace = text.find("{")
    end_brace = text.rfind("}")
    if start_brace != -1 and end_brace > start_brace:
        return text[start_brace : end_brace + 1].strip()

    return text


class StudyTutor:
    """Motor de IA para apoio ao estudo ativo, simulados e geração de decks Anki."""

    def __init__(self, solver: Optional[GeminiSolver] = None):
        self.solver = solver or GeminiSolver()

    async def answer_question(
        self,
        discipline: str,
        question: str,
        specific_material: Optional[str] = None
    ) -> Dict[str, Any]:
        """Responde a dúvidas conceituais citando expressamente os slides e apostilas do professor."""
        materials = get_course_study_materials(discipline, specific_material)
        context_text = extract_text_from_context_files(materials) if materials else ""

        system_instruction = (
            "Você é o Tutor Acadêmico e Monitor de Ensino da UFMG para estudantes de graduação.\n"
            "Seu papel é tirar dúvidas teóricas, conceituais e práticas com clareza exemplar, didática e rigor acadêmico.\n\n"
            "DIRETRIZES ESSENCIAIS:\n"
            "1. CITAÇÃO OBRIGATÓRIA DE SLIDES/PÁGINAS: Sempre que uma explicação ou conceito estiver presente nos materiais fornecidos, "
            "cite textualmente a referência exata (ex: '[Slide 14, Aula 03 - Fundamentos.pdf]' ou '[Página 22, Apostila]').\n"
            "2. Se o material fornecido não contiver o conceito específico, elucide o conteúdo com maestria conceitual, mas informe explicitamente: "
            "'*Nota: Este conceito específico não foi identificado de forma explícita nos slides arquivados da matéria, mas aqui está a explicação teórica padrão:*'\n"
            "3. Apresente fórmulas em notação matemática elegante (LaTeX ou Markdown com destaque).\n"
            "4. Forneça intuição física/prática, analogias quando couber e aponte armadilhas conceituais frequentes que alunos cometem em provas."
        )

        prompt_parts = []
        if context_text:
            prompt_parts.append(
                "--- MATERIAIS DIDÁTICOS E SLIDES DA DISCIPLINA ---\n"
                f"{context_text}\n"
                "--- FIM DOS MATERIAIS ---\n\n"
            )

        prompt_parts.append(
            f"Disciplina: {discipline}\n"
            f"Dúvida do Aluno: {question}\n\n"
            "Por favor, elabore uma resposta didática, profunda e estruturada, destacando pontos-chave e referenciando os slides/apostilas da matéria."
        )

        full_prompt = "".join(prompt_parts)
        response, used_model = await self.solver._generate_with_fallback(
            contents=[full_prompt],
            system_instruction=system_instruction,
            temperature=0.2
        )

        answer_text = response.text if response and response.text else "Não foi possível gerar a resposta."

        return {
            "answer": answer_text,
            "discipline": discipline,
            "question": question,
            "materials_used": [m.name for m in materials],
            "model_used": used_model
        }

    async def generate_flashcards(
        self,
        discipline: str,
        topic: Optional[str] = None,
        count: int = 8,
        specific_material: Optional[str] = None
    ) -> Dict[str, Any]:
        """Gera baralhos de flashcards para repetição espaçada e arquivo exportável para o Anki."""
        count = max(3, min(count, 15))
        materials = get_course_study_materials(discipline, specific_material)
        context_text = extract_text_from_context_files(materials) if materials else ""

        system_instruction = (
            "Você é um especialista em Aprendizagem Ativa e Repetição Espaçada (Spaced Repetition / Anki).\n"
            "Sua tarefa é criar flashcards de alto rendimento para revisão rápida de provas acadêmicas na UFMG.\n"
            "Cada flashcard deve conter:\n"
            "- 'front': Pergunta direta, definição-chave, fórmula ou comparação (máximo 2 linhas, instigante).\n"
            "- 'back': Resposta objetiva, precisa e direta ao ponto.\n"
            "- 'explanation': Breve explicação do porquê, pegadinha comum ou detalhe de aplicação.\n"
            "- 'source': Slide ou página de onde o conceito foi extraído (ex: 'Slide 15, Aula 4' ou 'Apostila Geral').\n\n"
            "RESPONDA ESTRITAMENTE COM UM ARRAY JSON VÁLIDO. Não inclua texto explicativo fora do JSON."
        )

        topic_str = f" focado no tópico '{topic}'" if topic else ""
        prompt = (
            f"Gere exatamente {count} flashcards de alta qualidade para a disciplina '{discipline}'{topic_str}.\n"
        )
        if context_text:
            prompt += f"\nBaseie-se nos seguintes materiais e slides do curso:\n{context_text}\n"

        prompt += (
            "\nFormato JSON esperado:\n"
            "[\n"
            '  {"front": "...", "back": "...", "explanation": "...", "source": "..."}\n'
            "]"
        )

        response, used_model = await self.solver._generate_with_fallback(
            contents=[prompt],
            system_instruction=system_instruction,
            temperature=0.3
        )

        raw_text = response.text if response and response.text else "[]"
        cleaned_json = clean_json_text(raw_text)

        cards = []
        try:
            parsed = json.loads(cleaned_json)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "front" in item and "back" in item:
                        cards.append({
                            "front": str(item.get("front", "")).strip(),
                            "back": str(item.get("back", "")).strip(),
                            "explanation": str(item.get("explanation", "")).strip(),
                            "source": str(item.get("source", "Material da Disciplina")).strip()
                        })
        except Exception as e:
            logger.warning(f"Erro ao parsear JSON de flashcards: {e}. Tentando fallback simples.")
            cards = [
                {
                    "front": f"Conceito Principal de {discipline}",
                    "back": "Revise as anotações principais e definições dos slides.",
                    "explanation": "Falha na decodificação do deck completo.",
                    "source": "Geral"
                }
            ]

        # Gera arquivo de importação do Anki (.txt com delimitador TAB)
        # O Anki aceita arquivos de texto com separador TAB e diretivas de cabeçalho
        anki_dir = settings.STORAGE_SUBMISSIONS_DIR / "flashcards"
        anki_dir.mkdir(parents=True, exist_ok=True)
        safe_disc = sanitize_filename(discipline)
        safe_topic = sanitize_filename(topic or "geral")
        anki_file = anki_dir / f"anki_{safe_disc}_{safe_topic}.txt"

        lines = [
            "#separator:tab",
            "#html:true",
            "#tags column:3",
        ]
        tag = f"UFMG::{safe_disc}"
        for c in cards:
            front = c["front"].replace("\t", " ").replace("\n", "<br>")
            back = c["back"].replace("\t", " ").replace("\n", "<br>")
            if c.get("explanation"):
                back += f"<br><br><i>💡 Dica/Pegadinha: {c['explanation']}</i>"
            if c.get("source"):
                back += f"<br><small style='color:gray'>📚 Ref: {c['source']}</small>"
            lines.append(f"{front}\t{back}\t{tag}")

        anki_file.write_text("\n".join(lines), encoding="utf-8")

        return {
            "discipline": discipline,
            "topic": topic or "Conteúdo Geral",
            "cards": cards,
            "anki_file_path": anki_file,
            "materials_used": [m.name for m in materials],
            "model_used": used_model
        }

    async def generate_quiz(
        self,
        discipline: str,
        num_questions: int = 5,
        topic: Optional[str] = None,
        specific_material: Optional[str] = None
    ) -> Dict[str, Any]:
        """Gera um simulado interativo com questões de múltipla escolha e pegadinhas reais de provas."""
        num_questions = max(3, min(num_questions, 10))
        materials = get_course_study_materials(discipline, specific_material)
        context_text = extract_text_from_context_files(materials) if materials else ""

        system_instruction = (
            "Você é um Professor e Avaliador experiente da UFMG.\n"
            "Sua meta é formular um Simulado Pré-Prova interativo para treinar estudantes de alto rendimento.\n\n"
            "REGRAS DE CONSTRUÇÃO DAS QUESTÕES:\n"
            "1. Cada questão DEVE ser de múltipla escolha com exatamente 4 alternativas: A, B, C, D.\n"
            "2. Exatamente UMA alternativa deve ser correta.\n"
            "3. As outras 3 alternativas devem ser 'distratores inteligentes' (pegadinhas comuns, erros conceituais típicos de alunos, "
            "ou detalhes de convenção de sinais/unidades).\n"
            "4. Forneça uma 'explanation' didática e profunda explicando: por que a correta está certa E qual era a pegadinha nas erradas.\n"
            "5. Cite a 'reference' (ex: 'Slide 12 da Aula 04' ou 'Apostila Unidade 2').\n\n"
            "RESPONDA ESTRITAMENTE COM UM ARRAY JSON VÁLIDO. Não inclua texto explicativo fora do JSON."
        )

        topic_str = f" focado no tópico '{topic}'" if topic else ""
        prompt = (
            f"Elabore exatamente {num_questions} questões de simulado para a disciplina '{discipline}'{topic_str}.\n"
        )
        if context_text:
            prompt += f"\nBaseie as questões e pegadinhas no seguinte material e slides da matéria:\n{context_text}\n"

        prompt += (
            "\nFormato JSON esperado:\n"
            "[\n"
            "  {\n"
            '    "id": 1,\n'
            '    "question": "Enunciado da questão...",\n'
            '    "options": {\n'
            '      "A": "Texto da alternativa A",\n'
            '      "B": "Texto da alternativa B",\n'
            '      "C": "Texto da alternativa C",\n'
            '      "D": "Texto da alternativa D"\n'
            "    },\n"
            '    "correct_option": "B",\n'
            '    "explanation": "Explicação detalhada...",\n'
            '    "reference": "Slide 18, Aula 5"\n'
            "  }\n"
            "]"
        )

        response, used_model = await self.solver._generate_with_fallback(
            contents=[prompt],
            system_instruction=system_instruction,
            temperature=0.3
        )

        raw_text = response.text if response and response.text else "[]"
        cleaned_json = clean_json_text(raw_text)

        questions = []
        try:
            parsed = json.loads(cleaned_json)
            if isinstance(parsed, list):
                for idx, item in enumerate(parsed, 1):
                    if isinstance(item, dict) and "question" in item and "options" in item:
                        opts = item.get("options", {})
                        if not isinstance(opts, dict):
                            opts = {"A": "Opção A", "B": "Opção B", "C": "Opção C", "D": "Opção D"}
                        correct = str(item.get("correct_option", "A")).strip().upper()
                        if correct not in ["A", "B", "C", "D"]:
                            correct = "A"

                        questions.append({
                            "id": idx,
                            "question": str(item.get("question", "")).strip(),
                            "options": {
                                "A": str(opts.get("A", "")).strip(),
                                "B": str(opts.get("B", "")).strip(),
                                "C": str(opts.get("C", "")).strip(),
                                "D": str(opts.get("D", "")).strip(),
                            },
                            "correct_option": correct,
                            "explanation": str(item.get("explanation", "")).strip(),
                            "reference": str(item.get("reference", "Material da Disciplina")).strip()
                        })
        except Exception as e:
            logger.warning(f"Erro ao parsear JSON de quiz: {e}. Gerando questão de contingência.")
            questions = [
                {
                    "id": 1,
                    "question": f"Qual a principal recomendação para estudar para a prova de {discipline}?",
                    "options": {
                        "A": "Decorar apenas a véspera",
                        "B": "Resolver exercícios e revisar slides com constância",
                        "C": "Ignorar os materiais do professor",
                        "D": "Não fazer simulados"
                    },
                    "correct_option": "B",
                    "explanation": "O estudo ativo contínuo com resolução de problemas é o método mais eficaz.",
                    "reference": "Guia Geral de Estudos"
                }
            ]

        return {
            "discipline": discipline,
            "topic": topic or "Conteúdo Geral",
            "questions": questions,
            "materials_used": [m.name for m in materials],
            "model_used": used_model
        }
