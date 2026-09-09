"""Automação interativa e humanizada de Questionários e Quizzes do Moodle UFMG.

Recursos principais:
1. Extração ao vivo dos enunciados e campos diretamente da tela de tentativa (attempt.php),
   substituindo cada lacuna por marcadores pontuais [[CAMPO_1]], [[CAMPO_2]]...
2. Resolução contextualizada com IA (escolha de palavra única sem barras, preenchendo 100% dos campos).
3. Simulação de cadência humana realista:
   - Digitação caractere por caractere (press_sequentially) com atrasos naturais (45-95ms).
   - Pausas de reflexão entre frases.
   - Garantia de tempo total de tentativa seguro (ex: 2.5 a 4 minutos) para evitar auditoria do Moodle.
"""

import asyncio
import json
import random
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.async_api import async_playwright
from rich.console import Console

from config.settings import settings
from src.auth.moodle_auth import MoodleAuth

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


def normalize_str(text: str) -> str:
    """Normaliza texto removendo acentos e pontuações para comparações."""
    if not text:
        return ""
    return unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("utf-8").strip().lower()


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


class MoodleQuizAutomator:
    """Controlador inteligente e humanizado para questionários do Moodle."""

    def __init__(self, auth: Optional[MoodleAuth] = None):
        self.auth = auth or MoodleAuth()
        self.screenshots_dir = Path("storage/submissions/screenshots")
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    async def _open_or_resume_attempt(self, page) -> bool:
        """Abre uma nova tentativa, refaz tentativa anterior ('Fazer uma outra tentativa') ou continua tentativa em aberto."""
        if "attempt.php" in page.url:
            return True

        console.print(f"[cyan]Localizando botões de início/retomada de tentativa em: {page.url}[/cyan]")

        # 1. Se já estiver no resumo da tentativa (summary.php)
        if "summary.php" in page.url:
            ret_btn = page.locator("button:has-text('Retornar à tentativa'), a:has-text('Retornar à tentativa'), input[value*='Retornar à tentativa']").first
            if await ret_btn.count() > 0:
                await ret_btn.click()
                await page.wait_for_load_state("networkidle")
                return True

        # 2. Seletores em ordem de prioridade (suporta refazer tentativa já respondida)
        attempt_selectors = [
            # Prioridade A: Botões explícitos para refazer tentativa quando já houver tentativa concluída
            "button:has-text('Fazer uma outra tentativa')",
            "button:has-text('Fazer outra tentativa')",
            "button:has-text('Refazer')",
            "button:has-text('Tentar novamente o questionário')",
            "button:has-text('Tentar novamente')",
            "button:has-text('Nova tentativa')",
            "a:has-text('Fazer uma outra tentativa')",
            "a:has-text('Fazer outra tentativa')",
            "a:has-text('Tentar novamente')",
            "input[value*='Fazer uma outra tentativa']",
            "input[value*='Fazer outra tentativa']",
            "input[value*='Tentar novamente']",

            # Prioridade B: Formulário padrão do Moodle startattempt.php
            "form[action*='startattempt.php'] button",
            "form[action*='startattempt.php'] input[type='submit']",
            "a[href*='startattempt.php']",

            # Prioridade C: Continuar tentativa em andamento
            "button:has-text('Continuar sua tentativa')",
            "button:has-text('Continuar a última tentativa')",
            "button:has-text('Continuar tentativa')",
            "a:has-text('Continuar sua tentativa')",
            "a:has-text('Continuar a última tentativa')",
            "input[value*='Continuar sua tentativa']",
            "input[value*='Continuar a última tentativa']",

            # Prioridade D: Primeira tentativa
            "button:has-text('Tentar responder o questionário agora')",
            "button:has-text('Responder o questionário')",
            "a:has-text('Tentar responder o questionário agora')",
            "a:has-text('Responder o questionário')",
            "input[value*='Tentar responder o questionário agora']",

            # Prioridade E: Seletores genéricos
            "button:has-text('tentativa')",
            "a.btn:has-text('tentativa')",
            "input[value*='tentativa']"
        ]

        attempt_btn = None
        for sel in attempt_selectors:
            loc = page.locator(sel)
            if await loc.count() > 0:
                first_loc = loc.first
                try:
                    if await first_loc.is_visible():
                        attempt_btn = first_loc
                        btn_txt = (await attempt_btn.inner_text()).strip() if hasattr(attempt_btn, "inner_text") else "Tentativa"
                        console.print(f"[green]✔ Botão de tentativa acionado: '{btn_txt}'[/green]")
                        break
                except Exception:
                    continue

        if not attempt_btn:
            console.print("[yellow]Aviso: Nenhum botão de tentativa visível localizado na página.[/yellow]")
            return False

        # Clica no botão para iniciar/refazer tentativa
        await attempt_btn.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.2)

        # Trata eventual modal de confirmação no Moodle ("Iniciar tentativa", "Começar tentativa", etc.)
        modal_confirm_selectors = [
            ".modal.show button:has-text('Iniciar tentativa')",
            ".modal.show button:has-text('Começar tentativa')",
            ".modal.show input[value*='Iniciar tentativa']",
            ".modal.show .modal-footer .btn-primary",
            ".modal.show button.btn-primary",
            "div[role='dialog'] button:has-text('Iniciar tentativa')",
            "div[role='dialog'] button:has-text('Começar tentativa')",
            "div[role='dialog'] button.btn-primary",
            ".moodle-dialogue-confirm input[value*='Iniciar tentativa']",
            ".moodle-dialogue-confirm button.btn-primary"
        ]
        for m_sel in modal_confirm_selectors:
            m_loc = page.locator(m_sel)
            if await m_loc.count() > 0:
                try:
                    if await m_loc.first.is_visible():
                        console.print(f"[cyan]Confirmando modal de início/reabertura de tentativa: '{await m_loc.first.inner_text()}'...[/cyan]")
                        await m_loc.first.click()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(1.2)
                        break
                except Exception:
                    continue

        return "attempt.php" in page.url or "summary.php" in page.url

    async def inspect_and_extract_quiz(self, quiz_url: str, on_log: Optional[Any] = None) -> Dict[str, Any]:
        """Acessa a tentativa do questionário e extrai os enunciados reais com marcadores pontuais."""
        console.print(f"[cyan]Inspecionando estrutura real do quiz no Moodle: {quiz_url}[/cyan]")
        await _emit_log(on_log, "Inspecionando tentativa do questionário no Moodle...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=self.auth.cookies_path)
            page = await context.new_page()

            try:
                await page.goto(quiz_url, wait_until="networkidle")

                # Se não estiver em attempt.php, abre nova tentativa, refaz tentativa ou continua existente
                if "attempt.php" not in page.url:
                    opened = await self._open_or_resume_attempt(page)
                    if not opened and "attempt.php" not in page.url:
                        await _emit_log(on_log, "❌ Não foi possível abrir ou refazer a tentativa do questionário no Moodle.")
                        return {
                            "success": False,
                            "error": "Não foi possível abrir ou refazer a tentativa do questionário no Moodle."
                        }

                await _emit_log(on_log, "Extraindo enunciados, campos e lacunas das questões...")

                # Extração estruturada do DOM com mapeamento pontual de campos e limpeza de ruídos de tela
                questions_data = await page.evaluate(r'''() => {
                    const questions = [];
                    const qNodes = document.querySelectorAll(".que");
                    let globalInputIdx = 1;

                    qNodes.forEach((q, qIndex) => {
                        const noText = q.querySelector(".info .no") ? q.querySelector(".info .no").innerText.trim() : `Questão ${qIndex + 1}`;
                        const contentEl = q.querySelector(".content") || q;
                        
                        const clone = contentEl.cloneNode(true);

                        // Remove todos os spans de acessibilidade, controles internos, botões e mensagens do Moodle
                        clone.querySelectorAll(
                            '.accesshide, .sr-only, .im-controls, input[type="submit"], input[type="button"], button, .comment, .feedback, .grading, .history'
                        ).forEach(el => el.remove());

                        const inputs = clone.querySelectorAll("input[type='text'], textarea");
                        const inputMap = [];

                        inputs.forEach(inp => {
                            const token = `[[CAMPO_${globalInputIdx}]]`;
                            inputMap.push({
                                token: token,
                                key: `CAMPO_${globalInputIdx}`,
                                name: inp.getAttribute("name"),
                                id: inp.getAttribute("id"),
                                index: globalInputIdx,
                                type: "text"
                            });
                            const textNode = document.createTextNode(` ${token} `);
                            inp.parentNode.replaceChild(textNode, inp);
                            globalInputIdx++;
                        });

                        // Menus suspensos / Comboboxes (select)
                        const selects = clone.querySelectorAll("select");
                        selects.forEach(sel => {
                            const token = `[[CAMPO_${globalInputIdx}]]`;
                            const optList = Array.from(sel.options)
                                .map(o => o.text.trim())
                                .filter(t => t && !t.toLowerCase().includes("escolher") && !t.toLowerCase().includes("choose") && !t.toLowerCase().includes("selecion"));
                            
                            inputMap.push({
                                token: token,
                                key: `CAMPO_${globalInputIdx}`,
                                name: sel.getAttribute("name"),
                                id: sel.getAttribute("id"),
                                index: globalInputIdx,
                                type: "select",
                                options: optList
                            });
                            const optsHint = optList.length > 0 ? ` (Opções disponíveis: ${optList.join(" | ")})` : "";
                            const textNode = document.createTextNode(` ${token}${optsHint} `);
                            sel.parentNode.replaceChild(textNode, sel);
                            globalInputIdx++;
                        });

                        // Opções de rádio (múltipla escolha)
                        const radioOptions = [];
                        const answerNode = q.querySelector(".answer");
                        if (answerNode) {
                            const radios = answerNode.querySelectorAll("input[type='radio']");
                            radios.forEach(r => {
                                const lbl = r.closest("div, label, tr");
                                radioOptions.push({
                                    name: r.getAttribute("name"),
                                    value: r.getAttribute("value"),
                                    text: lbl ? lbl.innerText.trim() : ""
                                });
                            });
                        }

                        // Limpeza de resíduos de texto do Moodle
                        let cleanText = clone.innerText.trim();
                        cleanText = cleanText.replace(/Texto (?:informativo|da questão)/gi, '');
                        cleanText = cleanText.replace(/Resposta \d+\s*Questão \d+/gi, '');
                        cleanText = cleanText.replace(/Verificar Questão \d+/gi, '');
                        cleanText = cleanText.replace(/Questão \d+\s*Anot/gi, 'Anot');
                        cleanText = cleanText.replace(/\n{3,}/g, '\n\n').trim();

                        // Se for apenas bloco informativo do Moodle (.que.description) sem campos a responder
                        const isInfoOnly = q.classList.contains("description") || (inputMap.length === 0 && radioOptions.length === 0 && !cleanText.includes("?"));

                        questions.push({
                            qIndex: qIndex + 1,
                            qNumberText: noText,
                            fullTextWithTokens: cleanText,
                            inputsCount: inputMap.length,
                            inputs: inputMap,
                            radios: radioOptions,
                            isInfoOnly: isInfoOnly
                        });
                    });

                    return questions;
                }''')

                total_inputs = sum(q.get("inputsCount", 0) for q in questions_data)
                console.print(f"[green]✔ Perguntas extraídas com sucesso! Total de campos/lacunas: {total_inputs}[/green]")
                await _emit_log(on_log, f"✔ Questões extraídas com sucesso! Total de campos/lacunas: {total_inputs}")

                return {
                    "success": True,
                    "attempt_url": page.url,
                    "total_inputs": total_inputs,
                    "questions": questions_data
                }

            except Exception as e:
                console.print(f"[red]Erro ao extrair questões do quiz: {e}[/red]")
                await _emit_log(on_log, f"❌ Erro ao extrair questões do quiz: {e}")
                return {
                    "success": False,
                    "error": str(e)
                }
            finally:
                await browser.close()

    async def _verify_all_questions_on_current_attempt(self, page, on_log: Optional[Any] = None) -> int:
        """Clica sequencialmente no botão 'Verificar' de cada questão ativa antes de submeter a tentativa."""
        console.print("[cyan]Verificando questões individualmente no Moodle antes do envio definitivo...[/cyan]")
        verified_count = 0
        max_clicks = 60
        clicks = 0

        while clicks < max_clicks:
            if "attempt.php" not in page.url:
                break

            verify_buttons = page.locator(
                "button.submit.btn:has-text('Verificar'):not([disabled]), "
                "button[name$='-submit']:has-text('Verificar'):not([disabled]), "
                "input.submit[value*='Verificar']:not([disabled]), "
                "input[type='submit'][value*='Verificar']:not([disabled])"
            )
            count = await verify_buttons.count()
            if count == 0:
                break

            btn_to_click = None
            btn_name = ""
            for i in range(count):
                b = verify_buttons.nth(i)
                if await b.is_visible() and await b.is_enabled():
                    btn_to_click = b
                    btn_name = (await b.get_attribute("name")) or f"btn_{i+1}"
                    break

            if not btn_to_click:
                break

            console.print(f"  ✔ [Verificar] Acionando botão da questão ({btn_name})...")
            await _emit_log(on_log, f"Acionando botão 'Verificar' da questão ({btn_name})...")
            try:
                await btn_to_click.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.4, 0.8))
                await btn_to_click.click()
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(600)
                verified_count += 1
                clicks += 1
            except Exception as click_err:
                console.print(f"  [yellow]Nota ao clicar em Verificar ({btn_name}): {click_err}[/yellow]")
                break

        console.print(f"[green]✔ Total de questões verificadas no Moodle nesta etapa: {verified_count}[/green]")
        if verified_count > 0:
            await _emit_log(on_log, f"✔ {verified_count} questão(ões) verificada(s) com sucesso no Moodle")
        return verified_count

    async def fill_and_submit_quiz(
        self,
        quiz_url: str,
        answers: Any,
        auto_submit: bool = True,
        min_duration_seconds: int = 180,
        on_log: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Preenche o questionário com digitação humana realista e garante tempo de tentativa seguro."""
        console.print(f"[cyan]Iniciando preenchimento humanizado do questionário: {quiz_url}[/cyan]")
        await _emit_log(on_log, "Iniciando preenchimento do questionário no Moodle...")
        start_time = time.time()

        # Converte respostas para um mapa chave-valor direto com múltiplos aliases
        answers_dict: Dict[str, str] = {}
        if isinstance(answers, dict):
            for k, v in answers.items():
                clean_k = str(k).upper().replace("[[", "").replace("]]", "").strip()
                answers_dict[clean_k] = str(v)
                q_m = re.search(r"^(?:QUEST[ÃA]O|Q)\s*(\d+)(?:_(\d+))?$", clean_k)
                if q_m:
                    num = q_m.group(1)
                    sub = q_m.group(2)
                    if sub:
                        answers_dict[f"Q{num}_{sub}"] = str(v)
                    else:
                        answers_dict[f"Q{num}"] = str(v)
                elif clean_k.isdigit():
                    answers_dict[f"Q{clean_k}"] = str(v)
        elif isinstance(answers, list):
            for item in answers:
                if isinstance(item, dict):
                    k = item.get("key") or item.get("field")
                    v = item.get("value")
                    if k is not None and v is not None:
                        clean_k = str(k).upper().replace("[[", "").replace("]]", "").strip()
                        answers_dict[clean_k] = str(v)
                        q_m = re.search(r"^(?:QUEST[ÃA]O|Q)\s*(\d+)(?:_(\d+))?$", clean_k)
                        if q_m:
                            num = q_m.group(1)
                            sub = q_m.group(2)
                            if sub:
                                answers_dict[f"Q{num}_{sub}"] = str(v)
                            else:
                                answers_dict[f"Q{num}"] = str(v)
                        elif clean_k.isdigit():
                            answers_dict[f"Q{clean_k}"] = str(v)
                    elif "question" in item:
                        q_num = item["question"]
                        ans_val = item.get("answer") or item.get("answers")
                        if isinstance(ans_val, list):
                            for s_idx, sv in enumerate(ans_val):
                                answers_dict[f"Q{q_num}_{s_idx+1}"] = str(sv)
                        else:
                            answers_dict[f"Q{q_num}"] = str(ans_val)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=self.auth.cookies_path)
            page = await context.new_page()

            try:
                await page.goto(quiz_url, wait_until="networkidle")

                # Se não estiver em attempt.php, abre nova tentativa, refaz tentativa ou continua existente
                if "attempt.php" not in page.url:
                    opened = await self._open_or_resume_attempt(page)
                    if not opened and "attempt.php" not in page.url:
                        await _emit_log(on_log, "❌ Não foi possível abrir tentativa no Moodle.")
                        return {
                            "success": False,
                            "error": "Não foi possível abrir ou refazer a tentativa do questionário no Moodle."
                        }

                total_filled = 0
                has_next = True
                global_input_idx = 1

                while has_next:
                    question_locators = page.locator(".que")
                    q_count = await question_locators.count()

                    for i in range(q_count):
                        q_el = question_locators.nth(i)
                        
                        no_text = ""
                        no_locator = q_el.locator(".info .no")
                        if await no_locator.count() > 0:
                            no_text = await no_locator.inner_text()
                        
                        num_match = re.search(r"\d+", no_text)
                        q_num = int(num_match.group(0)) if num_match else (i + 1)

                        # 1. Inputs de texto / Lacunas
                        text_inputs = q_el.locator("input[type='text'], textarea")
                        text_count = await text_inputs.count()

                        for t_idx in range(text_count):
                            inp = text_inputs.nth(t_idx)
                            token_key = f"CAMPO_{global_input_idx}"
                            
                            # Busca a resposta correspondente
                            target_val = answers_dict.get(token_key)
                            if not target_val:
                                target_val = answers_dict.get(f"Q{q_num}_{t_idx+1}")
                            if not target_val:
                                target_val = answers_dict.get(f"Q{q_num}")

                            # Limpa barras e escolhe apenas a primeira opção se vier com barra
                            if target_val and "/" in target_val:
                                options = [o.strip() for o in target_val.split("/") if o.strip()]
                                target_val = options[0] if options else target_val

                            if target_val:
                                target_val = target_val.strip()
                                # Foco e digitação simulada tecla a tecla
                                await inp.focus()
                                await inp.fill("") # Limpa valor anterior
                                await asyncio.sleep(random.uniform(0.2, 0.5))
                                await inp.press_sequentially(target_val, delay=random.randint(40, 85))
                                total_filled += 1
                                console.print(f"  ✔ [{token_key}] Preenchido com cadência humana: '{target_val}'")
                                await _emit_log(on_log, f"✔ [{token_key}] Preenchido: '{target_val[:25]}'")
                                
                                # Pausa natural entre preenchimentos (simulando leitura)
                                await asyncio.sleep(random.uniform(1.2, 3.0))

                            global_input_idx += 1

                        # 2. Alternativas de Múltipla Escolha (Radio buttons) - Imune ao embaralhamento do Moodle
                        radios = q_el.locator("input[type='radio']")
                        r_count = await radios.count()
                        if r_count > 0:
                            target_val = (
                                answers_dict.get(f"Q{q_num}") or
                                answers_dict.get(f"QUESTAO_{q_num}") or
                                answers_dict.get(f"QUESTAO {q_num}") or
                                answers_dict.get(str(q_num)) or
                                answers_dict.get(f"CAMPO_{global_input_idx}")
                            )
                            if target_val:
                                clean_target = str(target_val).strip()
                                clean_target = re.sub(r"^\s*-\s*\*\*Resposta:\*\*\s*", "", clean_target, flags=re.IGNORECASE).strip()
                                clean_target = re.sub(r"^\s*\*\*Resposta:\*\*\s*", "", clean_target, flags=re.IGNORECASE).strip()
                                clean_target = re.sub(r"^\s*Resposta:\s*", "", clean_target, flags=re.IGNORECASE).strip()

                                # Letra do alvo (se houver, ex: 'c' de 'c. texto' ou apenas 'C')
                                target_letter_match = re.match(r"^\s*([a-eA-E])(?:\.|\)|\:|\s|$)", clean_target)
                                target_letter = target_letter_match.group(1).lower() if target_letter_match else None

                                # Conteúdo textual do alvo (sem prefixo de letra)
                                target_content = re.sub(r"^\s*([a-eA-E])(?:\.|\)|\:|\s|-)+", "", clean_target).strip()
                                norm_target_content = normalize_str(target_content)

                                # Extrai as opções renderizadas ao vivo nesta tentativa no Moodle
                                candidate_options = []
                                for r_idx in range(r_count):
                                    r_inp = radios.nth(r_idx)
                                    r_id = await r_inp.get_attribute("id")
                                    opt_raw_text = ""

                                    if r_id:
                                        lbl_loc = q_el.locator(f"label[for='{r_id}']")
                                        if await lbl_loc.count() > 0:
                                            opt_raw_text = await lbl_loc.first.inner_text()

                                    if not opt_raw_text.strip():
                                        opt_raw_text = await r_inp.evaluate(
                                            """el => {
                                                const lbl = el.closest('label');
                                                if (lbl && lbl.innerText && lbl.innerText.trim()) return lbl.innerText;
                                                const container = el.closest('.r0, .r1, .answer, [class*="answer"], div.d-flex');
                                                if (container && container.innerText && container.innerText.trim()) return container.innerText;
                                                return '';
                                            }"""
                                        ) or ""

                                    clean_opt = opt_raw_text.replace("\n", " ").strip()
                                    clean_opt = re.sub(r"(?i)não respondido|marcado|selecionado", "", clean_opt).strip()

                                    opt_letter_match = re.search(r"(?:^|\s)([a-eA-E])(?:\.|\)|\:)\s*", clean_opt)
                                    opt_letter = opt_letter_match.group(1).lower() if opt_letter_match else None

                                    opt_content = re.sub(r"(?:^|\s)([a-eA-E])(?:\.|\)|\:)\s*", "", clean_opt).strip()
                                    norm_opt_content = normalize_str(opt_content)

                                    candidate_options.append({
                                        "locator": r_inp,
                                        "raw_text": clean_opt,
                                        "letter": opt_letter,
                                        "content": opt_content,
                                        "norm_content": norm_opt_content
                                    })

                                # Algoritmo de correspondência imune a embaralhamento
                                best_match = None
                                best_score = -1

                                for opt in candidate_options:
                                    score = 0
                                    # 1. Correspondência textual do conteúdo (imune a trocas de posição/letra)
                                    if len(norm_target_content) >= 3 and len(opt["norm_content"]) >= 3:
                                        if norm_target_content == opt["norm_content"]:
                                            score = 100
                                        elif norm_target_content in opt["norm_content"] and len(norm_target_content) >= 8:
                                            score = 90
                                        elif opt["norm_content"] in norm_target_content and len(opt["norm_content"]) >= 8:
                                            score = 90
                                        else:
                                            words_target = set(w for w in norm_target_content.split() if len(w) > 2)
                                            words_opt = set(w for w in opt["norm_content"].split() if len(w) > 2)
                                            if words_target and words_opt:
                                                overlap = len(words_target & words_opt)
                                                jaccard = overlap / max(len(words_target), len(words_opt))
                                                if jaccard >= 0.4:
                                                    score = 50 + int(jaccard * 35)

                                    # 2. Fallback de correspondência por letra exata (apenas se fornecida)
                                    if target_letter and opt["letter"] and target_letter == opt["letter"]:
                                        if len(norm_target_content) < 3:
                                            score = 80
                                        else:
                                            score += 15

                                    if score > best_score:
                                        best_score = score
                                        best_match = opt

                                if best_match and best_score >= 50:
                                    await asyncio.sleep(random.uniform(1.0, 2.2))
                                    await best_match["locator"].check()
                                    total_filled += 1
                                    console.print(
                                        f"  ✔ [Q{q_num}] Alternativa marcada com segurança (confiança {best_score}%): "
                                        f"[{best_match['letter'] or '?'}] {best_match['content'][:40]}"
                                    )
                                    await _emit_log(on_log, f"✔ [Q{q_num}] Marcada: [{best_match['letter'] or '?'}] {best_match['content'][:30]}")
                                else:
                                    console.print(
                                        f"  [yellow]⚠ [Q{q_num}] Nenhuma alternativa correspondeu com segurança a: '{clean_target}' (score: {best_score}). Mantendo desmarcado para segurança.[/yellow]"
                                    )

                        # 3. Menus Suspensos / Comboboxes (select) - Imune ao embaralhamento de linhas do Moodle
                        selects = q_el.locator("select")
                        s_count = await selects.count()
                        for s_idx in range(s_count):
                            sel_el = selects.nth(s_idx)
                            token_key = f"CAMPO_{global_input_idx}"

                            # Rótulo textual da linha/pergunta associada ao select
                            clean_row_label = await sel_el.evaluate('''el => {
                                const row = el.closest('tr, .form-inline, .row, div');
                                if (!row) return '';
                                const clone = row.cloneNode(true);
                                clone.querySelectorAll('select, .accesshide, .sr-only').forEach(e => e.remove());
                                return clone.innerText.trim();
                            }''')
                            clean_row_label = re.sub(r"(?i)resposta\s*\d+\s*quest[ãa]o\s*\d+", "", clean_row_label).strip()

                            # Opções disponíveis no menu
                            options_data = await sel_el.evaluate('''el => {
                                return Array.from(el.options).map(o => ({
                                    value: o.value,
                                    text: o.text.trim(),
                                    selected: o.selected
                                }));
                            }''')
                            valid_opts = [
                                o for o in options_data 
                                if o["text"] and not any(ign in o["text"].lower() for ign in ["escolher", "choose", "selecion"])
                            ]

                            # Identifica o valor alvo no answers_dict
                            target_val = None
                            
                            # 1. Se houver rótulo na linha (ex: "Gustave Eiffel"), busca no answers_dict quem associa com este rótulo
                            if clean_row_label:
                                norm_lbl = normalize_str(clean_row_label)
                                for k, v in answers_dict.items():
                                    val_str = str(v)
                                    parts = re.split(r"[→\->:]", val_str, maxsplit=1)
                                    if len(parts) == 2:
                                        left = parts[0].strip()
                                        right = parts[1].strip()
                                        if norm_lbl == normalize_str(left) or norm_lbl in normalize_str(left) or normalize_str(left) in norm_lbl:
                                            target_val = right
                                            break
                                    elif norm_lbl == normalize_str(str(k)) or norm_lbl in normalize_str(str(k)):
                                        target_val = val_str
                                        break

                            # 2. Se não encontrou por rótulo, tenta por Q{q_num}_{s_idx+1} ou token
                            if not target_val:
                                target_val = (
                                    answers_dict.get(f"Q{q_num}_{s_idx+1}") or 
                                    answers_dict.get(token_key) or 
                                    answers_dict.get(f"Q{q_num}")
                                )

                            # Se o target_val vier no formato "Nome → Resposta", extrai a resposta
                            if target_val and any(sep in str(target_val) for sep in ["→", "->"]):
                                parts = re.split(r"[→\->]", str(target_val), maxsplit=1)
                                target_val = parts[1].strip()

                            if target_val:
                                clean_target_val = str(target_val).strip().strip("*").strip()
                                norm_target = normalize_str(clean_target_val)

                                best_opt = None
                                best_score = -1
                                for opt in valid_opts:
                                    norm_opt = normalize_str(opt["text"])
                                    if norm_target == norm_opt:
                                        score = 100
                                    elif norm_target in norm_opt or norm_opt in norm_target:
                                        score = 85
                                    else:
                                        w_t = set(w for w in norm_target.split() if len(w) > 2)
                                        w_o = set(w for w in norm_opt.split() if len(w) > 2)
                                        overlap = len(w_t & w_o)
                                        score = int((overlap / max(len(w_t), len(w_o), 1)) * 70) if (w_t and w_o) else 0

                                    if score > best_score:
                                        best_score = score
                                        best_opt = opt

                                if best_opt and best_score >= 40:
                                    await asyncio.sleep(random.uniform(0.3, 0.7))
                                    await sel_el.select_option(value=best_opt["value"])
                                    await sel_el.dispatch_event("change")
                                    total_filled += 1
                                    console.print(
                                        f"  ✔ [Q{q_num} | {clean_row_label or token_key}] Combobox selecionada: '{best_opt['text']}'"
                                    )
                                    await _emit_log(on_log, f"✔ [Q{q_num}] Selecionada: '{best_opt['text'][:30]}'")
                                    await asyncio.sleep(random.uniform(0.8, 1.8))
                                else:
                                    console.print(
                                        f"  [yellow]⚠ [Q{q_num} | {clean_row_label}] Nenhuma opção correspondeu a '{clean_target_val}'.[/yellow]"
                                    )

                            global_input_idx += 1

                    # Se for submissão definitiva (auto_submit=True), aciona 'Verificar' em cada questão antes de avançar
                    if auto_submit:
                        await self._verify_all_questions_on_current_attempt(page, on_log=on_log)

                    # Avançar página ou finalizar
                    finish_btn = page.locator("input[type='submit'][value*='Finalizar tentativa'], button:has-text('Finalizar tentativa')")
                    next_btn = page.locator("input[type='submit'][value*='Próxima página'], button:has-text('Próxima página')")

                    if await finish_btn.count() > 0:
                        console.print("[dim]Página concluída. Indo para resumo da tentativa...[/dim]")
                        await finish_btn.first.click()
                        await page.wait_for_load_state("networkidle")
                        has_next = False
                    elif await next_btn.count() > 0:
                        console.print("[dim]Avançando para a próxima página do questionário...[/dim]")
                        await next_btn.first.click()
                        await page.wait_for_load_state("networkidle")
                    else:
                        has_next = False

                # 3. Na tela de resumo (summary.php)
                if "summary.php" in page.url or await page.locator("button:has-text('Enviar tudo e terminar')").count() > 0:
                    # Se for apenas preenchimento (auto_submit=False), salva o rascunho e retorna imediatamente
                    if not auto_submit:
                        console.print(f"[bold green]✔ Respostas salvas na tentativa no Moodle ({total_filled} campos preenchidos)![/bold green]")
                        await _emit_log(on_log, f"✔ Rascunho salvo! {total_filled} campo(s) preenchido(s) no Moodle.")
                        return {
                            "success": True,
                            "status": "draft_saved",
                            "total_filled": total_filled,
                            "summary_url": page.url,
                            "message": f"Questionário preenchido com sucesso ({total_filled} campos)! Respostas salvas como rascunho na sua tentativa para você conferir no Moodle."
                        }

                    # Se for submissão definitiva (auto_submit=True), simula a janela de tempo seguro
                    elapsed = time.time() - start_time
                    target_duration = max(min_duration_seconds, total_filled * 8)
                    
                    if elapsed < target_duration:
                        wait_seconds = int(target_duration - elapsed)
                        console.print(
                            f"[bold yellow]⏳ Simulando tempo de leitura e revisão humana do aluno "
                            f"(aguardando {wait_seconds}s para atingir tempo seguro de {int(target_duration)}s no Moodle)...[/bold yellow]"
                        )
                        while wait_seconds > 0:
                            step = min(wait_seconds, 15)
                            await asyncio.sleep(step)
                            wait_seconds -= step
                            if wait_seconds > 0:
                                console.print(f"[dim]⏳ Revisando tentativa... faltam {wait_seconds}s para submissão definitiva...[/dim]")
                                await _emit_log(on_log, f"Aguardando cadência humana: faltam {wait_seconds}s para envio seguro...")

                    console.print("[bold green]Confirmando envio definitivo no Moodle...[/bold green]")
                    await _emit_log(on_log, "Confirmando 'Enviar tudo e terminar' no Moodle...")
                    submit_button = page.locator("button:has-text('Enviar tudo e terminar'), input[value*='Enviar tudo e terminar']").first
                    await submit_button.click()
                    await page.wait_for_timeout(1000)

                    # Confirma no diálogo modal do Moodle
                    confirm_btn = page.locator(
                        ".modal.show button[data-action='confirm'], "
                        ".modal.show button.btn-primary:has-text('Enviar tudo e terminar'), "
                        ".moodle-dialogue-confirm input[value*='Enviar tudo e terminar'], "
                        "div[role='dialog'] button:has-text('Enviar tudo e terminar')"
                    )

                    if await confirm_btn.count() > 0:
                        await confirm_btn.first.click()
                    else:
                        await page.keyboard.press("Enter")

                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(2500)

                    total_time_str = f"{int(time.time() - start_time)}s"
                    screenshot_path = self.screenshots_dir / f"quiz_submitted_{int(time.time())}.png"
                    await page.screenshot(path=str(screenshot_path), full_page=False)

                    grade_info = await page.evaluate('''() => {
                        const stateEl = document.querySelector(".cell.c1, .generaltable td");
                        const gradeEl = document.querySelector(".cell.c2, .feedback");
                        return {
                            stateText: stateEl ? stateEl.innerText.trim() : "",
                            gradeText: gradeEl ? gradeEl.innerText.trim() : ""
                        };
                    }''')

                    console.print(f"[bold green]✔ Questionário submetido com sucesso no Moodle! Tempo total empregado: {total_time_str}[/bold green]")
                    await _emit_log(on_log, f"✔ Questionário submetido com sucesso! Tempo: {total_time_str} | Nota: {grade_info.get('gradeText', 'N/A')}")

                    return {
                        "success": True,
                        "status": "submitted",
                        "total_filled": total_filled,
                        "elapsed_time": total_time_str,
                        "review_url": page.url,
                        "screenshot_path": str(screenshot_path),
                        "grade_info": grade_info
                    }

                return {
                    "success": True,
                    "status": "completed",
                    "total_filled": total_filled
                }

            except Exception as e:
                console.print(f"[red]Erro na automação do questionário: {e}[/red]")
                await _emit_log(on_log, f"❌ Erro na automação do questionário: {e}")
                return {
                    "success": False,
                    "error": str(e)
                }
            finally:
                await browser.close()

    async def finalize_submitted_quiz(self, quiz_url: str, on_log: Optional[Any] = None) -> Dict[str, Any]:
        """Acessa a tentativa salva e clica em 'Enviar tudo e terminar' no Moodle."""
        console.print(f"[cyan]Finalizando submissão do questionário no Moodle: {quiz_url}[/cyan]")
        await _emit_log(on_log, "Acessando tentativa salva para finalização no Moodle...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=self.auth.cookies_path)
            page = await context.new_page()
            try:
                await page.goto(quiz_url, wait_until="networkidle")

                if "summary.php" not in page.url and "attempt.php" not in page.url:
                    await self._open_or_resume_attempt(page)

                # Se estiver na tela de resumo (summary.php), retorna à tentativa para poder verificar as questões
                if "summary.php" in page.url:
                    return_btn = page.locator("a:has-text('Retornar à tentativa'), button:has-text('Retornar à tentativa'), a[href*='attempt.php']").first
                    if await return_btn.count() > 0:
                        console.print("[dim]Retornando à tentativa para acionar 'Verificar' em cada questão pendente...[/dim]")
                        await _emit_log(on_log, "Retornando à tentativa para acionar 'Verificar' nas questões...")
                        await return_btn.click()
                        await page.wait_for_load_state("networkidle")

                # Na tentativa, percorre as páginas acionando 'Verificar' em todas as questões pendentes
                if "attempt.php" in page.url:
                    has_next_page = True
                    while has_next_page:
                        await self._verify_all_questions_on_current_attempt(page, on_log=on_log)

                        next_page_btn = page.locator("input[type='submit'][value*='Próxima página'], button:has-text('Próxima página')")
                        if await next_page_btn.count() > 0 and await next_page_btn.first.is_visible():
                            console.print("[dim]Avançando para a próxima página para verificar questões...[/dim]")
                            await next_page_btn.first.click()
                            await page.wait_for_load_state("networkidle")
                        else:
                            has_next_page = False

                    finish_btn = page.locator("input[type='submit'][value*='Finalizar tentativa'], button:has-text('Finalizar tentativa')")
                    if await finish_btn.count() > 0:
                        console.print("[dim]Todas as questões verificadas. Indo para resumo da tentativa...[/dim]")
                        await finish_btn.first.click()
                        await page.wait_for_load_state("networkidle")

                submit_button = page.locator("button:has-text('Enviar tudo e terminar'), input[value*='Enviar tudo e terminar']").first
                if await submit_button.count() > 0:
                    await _emit_log(on_log, "Confirmando 'Enviar tudo e terminar' no Moodle...")
                    await submit_button.click()
                    await page.wait_for_timeout(1000)

                    confirm_btn = page.locator(
                        ".modal.show button[data-action='confirm'], "
                        ".modal.show button.btn-primary:has-text('Enviar tudo e terminar'), "
                        ".moodle-dialogue-confirm input[value*='Enviar tudo e terminar'], "
                        "div[role='dialog'] button:has-text('Enviar tudo e terminar')"
                    )
                    if await confirm_btn.count() > 0:
                        await confirm_btn.first.click()
                    else:
                        await page.keyboard.press("Enter")

                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(2500)

                    screenshot_path = self.screenshots_dir / f"quiz_submitted_{int(time.time())}.png"
                    await page.screenshot(path=str(screenshot_path), full_page=False)

                    grade_info = await page.evaluate('''() => {
                        const stateEl = document.querySelector(".cell.c1, .generaltable td");
                        const gradeEl = document.querySelector(".cell.c2, .feedback");
                        return {
                            stateText: stateEl ? stateEl.innerText.trim() : "",
                            gradeText: gradeEl ? gradeEl.innerText.trim() : ""
                        };
                    }''')

                    msg = "Questionário finalizado e submetido com sucesso no Moodle!"
                    if grade_info.get("gradeText"):
                        msg += f" (Nota: {grade_info.get('gradeText')})"
                    await _emit_log(on_log, f"✔ {msg}")

                    return {
                        "success": True,
                        "status": "submitted",
                        "review_url": page.url,
                        "screenshot_path": str(screenshot_path),
                        "grade_info": grade_info,
                        "message": msg
                    }

                return {
                    "success": False,
                    "error": "Não foi encontrado botão para 'Enviar tudo e terminar' no Moodle."
                }

            except Exception as e:
                await _emit_log(on_log, f"❌ Erro ao finalizar questionário: {e}")
                return {
                    "success": False,
                    "error": str(e)
                }
            finally:
                await browser.close()
