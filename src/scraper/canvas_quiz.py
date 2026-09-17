"""Automação interativa e humanizada de Questionários e Quizzes do Canvas LMS.

Executa inspeção ao vivo via Playwright para extrair enunciados reais, blocos de
código, opções de múltipla escolha e campos de texto, preenche respostas com digitação
humana e submete ou salva como rascunho.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from playwright.async_api import Page, async_playwright
from rich.console import Console

from config.settings import settings
from src.auth.canvas_auth import CanvasAuth

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


class CanvasQuizAutomator:
    """Controlador inteligente para inspeção, preenchimento e submissão de Quizzes do Canvas LMS."""

    def __init__(self, auth: Optional[CanvasAuth] = None):
        self.auth = auth or CanvasAuth()

    async def _ensure_quiz_take_page(self, page: Page, quiz_url: str) -> bool:
        """Navega até a tela ativa de realização do questionário (/take)."""
        clean_url = quiz_url.strip()
        try:
            await page.goto(clean_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                await page.goto(clean_url, timeout=30000)
            except Exception as e:
                console.print(f"[yellow]Aviso ao carregar URL do Canvas: {e}[/yellow]")

        await asyncio.sleep(1.5)

        # Se for redirecionado para a página de login do Canvas
        if any(k in page.url.lower() for k in ["/login", "login.jsp", "sso", "saml"]):
            console.print("[yellow]⚠️ Redirecionado para tela de login do Canvas. Verificando credenciais salvas...[/yellow]")
            if getattr(settings, "CANVAS_USERNAME", "") and getattr(settings, "CANVAS_PASSWORD", ""):
                ok, _ = await self.auth.login_with_credentials(headless=True)
                if ok:
                    await page.goto(clean_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1.5)
            if any(k in page.url.lower() for k in ["/login", "login.jsp", "sso", "saml"]):
                console.print("[red]❌ Sessão do Canvas LMS expirada e credenciais não configuradas ou inválidas.[/red]")
                return False

        # Se for uma página de Assignment que aponta para um Quiz, procura o link direto
        if "/assignments/" in page.url and "/quizzes/" not in page.url:
            quiz_link = page.locator("a[href*='/quizzes/']").first
            if await quiz_link.count() > 0:
                try:
                    href = await quiz_link.get_attribute("href")
                    if href:
                        if href.startswith("/"):
                            base = self.auth.base_url.rstrip("/")
                            href = f"{base}{href}"
                        await page.goto(href, wait_until="domcontentloaded", timeout=25000)
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

        # Verifica se já estamos na tela de realização de questões
        if "/take" in page.url or await page.locator("#submit_quiz_form, .question, .quiz_sortable").count() > 0:
            return True

        # Procura botões para iniciar, retomar ou refazer o questionário
        start_button_selectors = [
            "#take_quiz_link",
            "a#take_quiz_link",
            "button#take_quiz_link",
            ".take_quiz_button",
            "a.take_quiz_button",
            "a[href*='/take']",
            "form#take_quiz_form button[type='submit']",
            "form#take_quiz_form input[type='submit']",
            "form[action*='/take'] button",
            "a.btn-primary:has-text('Quiz')",
            "button.btn-primary:has-text('Quiz')",
            "a:has-text('Take the Quiz')",
            "a:has-text('Resume Quiz')",
            "a:has-text('Take the Quiz Again')",
            "a:has-text('Fazer o teste')",
            "a:has-text('Responder ao questionário')",
            "a:has-text('Retomar teste')",
            "a:has-text('Continuar teste')",
            "a:has-text('Fazer o teste novamente')",
            "button:has-text('Take the Quiz')",
            "button:has-text('Resume Quiz')",
            "button:has-text('Fazer o teste')",
            "button:has-text('Retomar teste')",
        ]

        for sel in start_button_selectors:
            loc = page.locator(sel)
            if await loc.count() > 0:
                try:
                    first_btn = loc.first
                    if await first_btn.is_visible():
                        console.print(f"[cyan]Iniciando/retomando Quiz no Canvas via botão: '{sel}'...[/cyan]")
                        await first_btn.click()
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                        await asyncio.sleep(2.0)
                        break
                except Exception:
                    continue

        # Confirma modais de início / atenção se houverem (ex: dialog 'Attention!' com botão 'Begin')
        # Prioriza seletores de estrutura DOM independentes de idioma, com fallbacks de texto
        modal_confirm_selectors = [
            ".ui-dialog:visible .ui-dialog-buttonset button",
            ".ui-dialog:visible .ui-dialog-buttonpane button:not(.ui-dialog-titlebar-close)",
            ".ui-dialog:visible button.btn-primary",
            ".ui-dialog:visible button:not(.ui-dialog-titlebar-close)",
            "div[role='dialog']:visible .ui-dialog-buttonset button",
            "div[role='dialog']:visible button.btn-primary",
            "div[role='dialog']:visible button:not(.ui-dialog-titlebar-close):not([aria-label*='Close'])",
            "button:has-text('Begin')",
            "button:has-text('Take the Quiz')",
            "button:has-text('Resume Quiz')",
            "button:has-text('Start')",
            "button:has-text('Continue')",
            "button:has-text('Fazer o teste')",
            "button:has-text('Iniciar')",
            "button:has-text('Começar')",
            "button:has-text('Continuar')",
            "button:has-text('Retomar')",
        ]

        for _ in range(6):  # aguarda até 3 segundos caso o modal anime na tela
            if "/take" in page.url or await page.locator("#submit_quiz_form, .question, .quiz_sortable, #questions, .display_question").count() > 0:
                break

            clicked_modal = False
            for m_sel in modal_confirm_selectors:
                m_loc = page.locator(m_sel)
                if await m_loc.count() > 0:
                    for idx in range(await m_loc.count()):
                        btn = m_loc.nth(idx)
                        try:
                            if await btn.is_visible():
                                b_text = (await btn.inner_text()).strip()
                                console.print(f"[cyan]Confirmando modal do Canvas via '{m_sel}' ('{b_text}')...[/cyan]")
                                await btn.click()
                                try:
                                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                except Exception:
                                    pass
                                await asyncio.sleep(2.0)
                                clicked_modal = True
                                break
                        except Exception:
                            continue
                if clicked_modal:
                    break

            if clicked_modal:
                break
            await asyncio.sleep(0.5)

        take_indicators = [
            "/take" in page.url,
            await page.locator("#submit_quiz_form").count() > 0,
            await page.locator(".question").count() > 0,
            await page.locator("#questions").count() > 0,
            await page.locator(".quiz_sortable").count() > 0,
            await page.locator(".display_question").count() > 0,
            await page.locator("#submit_quiz_button").count() > 0,
            await page.locator("button.next-question").count() > 0,
        ]
        return any(take_indicators)

    async def inspect_and_extract_quiz(self, quiz_url: str, on_log: Optional[Any] = None) -> Dict[str, Any]:
        """Acessa a tentativa do Quiz no Canvas e extrai enunciados reais, códigos e alternativas."""
        if not self.auth.session_exists:
            console.print(f"[yellow]Aviso: Sessão do Canvas não encontrada em {self.auth.cookies_path}.[/yellow]")
            await _emit_log(on_log, "Aviso: Sessão local do Canvas não encontrada. Prosseguindo com resolução direta...")
            return {
                "success": False,
                "error": "Sessão do Canvas não encontrada.",
                "questions": []
            }

        p = None
        browser = None
        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(self.auth.cookies_path))
            page = await context.new_page()

            await _emit_log(on_log, "Conectando ao Canvas LMS e abrindo questionário...")
            opened = await self._ensure_quiz_take_page(page, quiz_url)
            if not opened:
                if any(k in page.url.lower() for k in ["/login", "login.jsp", "sso", "saml"]):
                    err_msg = (
                        "Sessão do Canvas LMS expirada (tela de login detectada). "
                        "Renove sua sessão com o comando /login no Discord ou execute 'python -m src.auth.canvas_auth'."
                    )
                    await _emit_log(on_log, f"⚠️ {err_msg}")
                    return {
                        "success": False,
                        "error": err_msg,
                        "questions": []
                    }
                # Tenta verificar se o quiz já está submetido / finalizado
                is_finished = await page.locator(".quiz_score, .quiz-submission, #quiz_summary, .submission-details").count() > 0
                if is_finished:
                    await _emit_log(on_log, "ℹ️ Este questionário já foi finalizado no Canvas.")
                    return {
                        "success": False,
                        "error": "Questionário já submetido e finalizado no Canvas.",
                        "is_finished": True,
                        "questions": []
                    }
                await _emit_log(on_log, "❌ Não foi possível abrir ou retomar a tentativa do quiz no Canvas.")
                return {
                    "success": False,
                    "error": "Não foi possível abrir ou retomar a tentativa do quiz no Canvas.",
                    "questions": []
                }

            await _emit_log(on_log, "Extraindo enunciados, códigos Python, lacunas e alternativas do Canvas...")

            # Extração estruturada do DOM do Canvas
            questions_data = await page.evaluate(r'''() => {
                const questions = [];
                // Identifica apenas os contêineres reais de questão
                let qNodes = Array.from(document.querySelectorAll(".question.display_question"));
                if (qNodes.length === 0) {
                    qNodes = Array.from(document.querySelectorAll("#questions .question, .quiz_sortable .question_holder .question"));
                }
                
                let globalInputIdx = 1;

                qNodes.forEach((q, qIndex) => {
                    const idAttr = q.getAttribute("id") || `question_${qIndex + 1}`;
                    const qCanvasId = idAttr.replace("question_", "");

                    // Título / Número da questão
                    const nameEl = q.querySelector(".question_name, .name, .header .name");
                    const nameText = nameEl ? nameEl.innerText.trim() : `Questão ${qIndex + 1}`;

                    // Pontos
                    const pointsEl = q.querySelector(".question_points, .points, .header .points");
                    const pointsText = pointsEl ? pointsEl.innerText.trim() : "";

                    // Identificação do tipo da questão
                    const isMatching = q.classList.contains("matching_question");
                    const isMultipleAnswers = q.classList.contains("multiple_answers_question");
                    const isMultipleChoice = q.classList.contains("multiple_choice_question") || q.querySelectorAll("input[type='radio']").length > 0;
                    const isFillBlank = q.classList.contains("fill_in_multiple_blanks_question") || q.classList.contains("short_answer_question");
                    const isEssay = q.classList.contains("essay_question");

                    let qType = "other";
                    if (isMatching) qType = "matching_question";
                    else if (isMultipleAnswers) qType = "multiple_answers";
                    else if (isMultipleChoice) qType = "multiple_choice";
                    else if (isFillBlank) qType = "fill_in_the_blank";
                    else if (isEssay) qType = "essay";

                    // Enunciado limpo da questão (exclui especificamente blocos de edição do professor)
                    const textEl = q.querySelector(".question_text");
                    let promptText = "";
                    if (textEl) {
                        const clone = textEl.cloneNode(true);
                        clone.querySelectorAll(".screenreader-only, .accessibility_warning, .original_question_text, button, style, script").forEach(e => e.remove());
                        
                        // Preserva formatação de blocos de código
                        clone.querySelectorAll("pre, code").forEach(cb => {
                            cb.replaceWith(document.createTextNode(`\n\`\`\`\n${cb.innerText}\n\`\`\`\n`));
                        });
                        promptText = clone.innerText.replace(/\n{3,}/g, '\n\n').trim();
                    }

                    let fullText = promptText;
                    const inputMap = [];
                    const radioOptions = [];
                    const checkboxOptions = [];

                    // 1. Múltipla Escolha (Radios)
                    const radioNodes = Array.from(q.querySelectorAll(".answers .answer input[type='radio']"));
                    if (radioNodes.length > 0) {
                        fullText += "\n\nAlternativas:";
                        radioNodes.forEach((r, rIdx) => {
                            const rId = r.getAttribute("id");
                            const row = r.closest(".answer") || q;
                            const labelEl = row.querySelector(".answer_label, .answer_text, label");
                            const optText = labelEl ? labelEl.innerText.trim() : (row.innerText.trim() || `Opção ${rIdx + 1}`);
                            const letter = String.fromCharCode(97 + rIdx); // a, b, c, d
                            
                            radioOptions.push({
                                id: rId,
                                name: r.getAttribute("name"),
                                value: r.getAttribute("value"),
                                letter: letter,
                                text: optText,
                                checked: r.checked
                            });
                            fullText += `\n${letter}) ${optText}`;
                        });
                    }

                    // 2. Caixas de Seleção (Checkboxes)
                    const checkNodes = Array.from(q.querySelectorAll(".answers .answer input[type='checkbox']"));
                    if (checkNodes.length > 0) {
                        fullText += "\n\nOpções (marque todas as corretas):";
                        checkNodes.forEach((c, cIdx) => {
                            const cId = c.getAttribute("id");
                            const row = c.closest(".answer") || q;
                            const labelEl = row.querySelector(".answer_label, .answer_text, label");
                            const optText = labelEl ? labelEl.innerText.trim() : (row.innerText.trim() || `Opção ${cIdx + 1}`);
                            const letter = String.fromCharCode(97 + cIdx);

                            checkboxOptions.push({
                                id: cId,
                                name: c.getAttribute("name"),
                                value: c.getAttribute("value"),
                                letter: letter,
                                text: optText,
                                checked: c.checked
                            });
                            fullText += `\n[ ] ${optText}`;
                        });
                    }

                    // 3. Correspondência / Associação (Matching Question com selects)
                    if (isMatching) {
                        const answerRows = Array.from(q.querySelectorAll(".answers .answer"));
                        const items = [];
                        const allOptions = [];

                        answerRows.forEach(row => {
                            const labelEl = row.querySelector("label");
                            const selectEl = row.querySelector("select");
                            if (labelEl && selectEl) {
                                const term = labelEl.innerText.trim();
                                const token = `[[CAMPO_${globalInputIdx}]]`;
                                inputMap.push({
                                    token: token,
                                    key: `CAMPO_${globalInputIdx}`,
                                    select_name: selectEl.getAttribute("name"),
                                    select_id: selectEl.getAttribute("id"),
                                    term: term,
                                    type: "select"
                                });
                                items.push(`- ${term}: ${token}`);
                                globalInputIdx++;

                                Array.from(selectEl.options).forEach(opt => {
                                    const optT = opt.innerText.trim();
                                    if (optT && !optT.includes("[ Choose ]") && !optT.includes("[ Escolha ]") && !allOptions.includes(optT)) {
                                        allOptions.push(optT);
                                    }
                                });
                            }
                        });

                        if (items.length > 0) {
                            fullText += "\n\nAssociações / Correspondência:\n" + items.join("\n");
                        }
                        if (allOptions.length > 0) {
                            fullText += "\n\nOpções disponíveis para associação:\n" + allOptions.map(o => `- ${o}`).join("\n");
                        }
                    }

                    // 4. Lacunas / Preenchimento de texto (Inputs visíveis, ignorando editores ocultos)
                    if (isFillBlank || isEssay) {
                        const visibleInputs = Array.from(q.querySelectorAll(".answers input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='submit']), .answers textarea, .question_text input:not([type='hidden']), .question_text textarea"));
                        visibleInputs.forEach(inp => {
                            if (inp.offsetParent === null) return; // ignora elementos invisíveis
                            const token = `[[CAMPO_${globalInputIdx}]]`;
                            inputMap.push({
                                token: token,
                                key: `CAMPO_${globalInputIdx}`,
                                name: inp.getAttribute("name"),
                                id: inp.getAttribute("id"),
                                type: inp.tagName.toLowerCase() === "textarea" ? "textarea" : "text"
                            });
                            fullText += `\nCampo: ${token}`;
                            globalInputIdx++;
                        });
                    }

                    let parsedNumber = qIndex + 1;
                    const numMatch = nameText.match(/\d+/);
                    if (numMatch) {
                        parsedNumber = parseInt(numMatch[0], 10);
                    }

                    questions.push({
                        canvas_id: qCanvasId,
                        number: parsedNumber,
                        qNumberText: nameText,
                        name: nameText,
                        points: pointsText,
                        prompt: promptText,
                        fullTextWithTokens: fullText,
                        text: fullText,
                        inputs: inputMap,
                        radio_options: radioOptions,
                        checkbox_options: checkboxOptions,
                        question_type: qType
                    });
                });

                return questions;
            }''')

            await _emit_log(on_log, f"✔ {len(questions_data)} questões identificadas no Canvas!")
            return {
                "success": bool(questions_data),
                "questions": questions_data,
                "quiz_url": page.url
            }

        except Exception as e:
            console.print(f"[red]Erro ao extrair quiz do Canvas: {e}[/red]")
            await _emit_log(on_log, f"❌ Erro ao extrair quiz do Canvas: {e}")
            return {
                "success": False,
                "error": str(e),
                "questions": []
            }
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if p:
                try:
                    await p.stop()
                except Exception:
                    pass

    async def fill_and_submit_quiz(
        self,
        quiz_url: str,
        answers: Any,
        auto_submit: bool = False,
        on_log: Optional[Any] = None
    ) -> Tuple[bool, str]:
        """Preenche o questionário no Canvas de forma humanizada e salva ou submete."""
        if not self.auth.session_exists:
            return False, "Sessão do Canvas não encontrada em storage/cookies/canvas_session.json."

        # Normaliza respostas recebidas
        answers_dict: Dict[str, str] = {}
        if isinstance(answers, dict):
            for k, v in answers.items():
                clean_k = str(k).upper().replace("[[", "").replace("]]", "").strip()
                answers_dict[clean_k] = str(v).strip()
        elif isinstance(answers, list):
            for item in answers:
                if isinstance(item, dict):
                    k = item.get("key") or item.get("field") or item.get("question")
                    v = item.get("value") or item.get("answer")
                    if k is not None and v is not None:
                        clean_k = str(k).upper().replace("[[", "").replace("]]", "").strip()
                        answers_dict[clean_k] = str(v).strip()

        p = None
        browser = None
        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(self.auth.cookies_path))
            page = await context.new_page()

            await _emit_log(on_log, "Acessando tentativa do questionário no Canvas...")
            opened = await self._ensure_quiz_take_page(page, quiz_url)
            if not opened:
                if any(k in page.url.lower() for k in ["/login", "login.jsp", "sso", "saml"]):
                    return False, "Sessão do Canvas LMS expirada (tela de login detectada). Renove sua sessão com o comando /login no Discord ou execute 'python -m src.auth.canvas_auth'."
                return False, "Não foi possível abrir a tentativa de preenchimento do quiz no Canvas."

            await _emit_log(on_log, "Iniciando preenchimento humanizado das questões no Canvas...")

            total_filled = 0
            campo_counter = 1
            max_pages = 25
            current_page = 0

            while current_page < max_pages:
                current_page += 1

                # Mapeia apenas os contêineres reais de questões na tela atual
                q_locators = page.locator(".question.display_question")
                if await q_locators.count() == 0:
                    q_locators = page.locator("#questions .question, .quiz_sortable .question_holder .question")

                q_count = await q_locators.count()
                if q_count == 0:
                    break

                for idx in range(q_count):
                    q_el = q_locators.nth(idx)
                    q_num = idx + 1
                    try:
                        name_el = q_el.locator(".question_name, .name, .header .name").first
                        if await name_el.count() > 0:
                            name_str = await name_el.inner_text()
                            m = re.search(r'\d+', name_str)
                            if m:
                                q_num = int(m.group(0))
                    except Exception:
                        pass
                    q_name = f"Q{q_num}"

                    # 1. Rádios (Múltipla escolha)
                    radios = q_el.locator(".answers .answer input[type='radio']")
                    r_count = await radios.count()
                    if r_count > 0:
                        target_val = (
                            answers_dict.get(q_name)
                            or answers_dict.get(f"QUESTAO_{q_num}")
                            or answers_dict.get(f"QUESTAO{q_num}")
                            or answers_dict.get(str(q_num))
                            or ""
                        )

                        chosen = False
                        if target_val:
                            norm_target = target_val.lower().strip()
                            for r_idx in range(r_count):
                                r_input = radios.nth(r_idx)
                                r_id = await r_input.get_attribute("id")
                                lbl_text = ""
                                if r_id:
                                    lbl = q_el.locator(f"#{r_id}_label, label[for='{r_id}']")
                                    if await lbl.count() > 0:
                                        lbl_text = (await lbl.first.inner_text()).lower().strip()
                                if not lbl_text:
                                    ans_wrap = r_input.locator("..")
                                    lbl_text = (await ans_wrap.inner_text()).lower().strip()

                                opt_letter = chr(97 + r_idx)  # 'a', 'b', 'c', 'd'
                                if (norm_target == lbl_text
                                    or (len(norm_target) > 2 and norm_target in lbl_text)
                                    or (len(lbl_text) > 2 and lbl_text in norm_target)
                                    or norm_target.startswith(f"{opt_letter})")
                                    or norm_target.startswith(f"{opt_letter}.")
                                    or norm_target == opt_letter):
                                    await r_input.check(force=True)
                                    chosen = True
                                    total_filled += 1
                                    await _emit_log(on_log, f"✔ Questão {q_num}: Alternativa selecionada ({lbl_text[:35]}...)")
                                    break

                        # Se não encontrou pelo nome da questão, tenta casar com o texto de qualquer resposta
                        if not chosen:
                            for k, v in answers_dict.items():
                                if v:
                                    norm_v = str(v).lower().strip()
                                    for r_idx in range(r_count):
                                        r_input = radios.nth(r_idx)
                                        r_id = await r_input.get_attribute("id")
                                        lbl_text = ""
                                        if r_id:
                                            lbl = q_el.locator(f"#{r_id}_label, label[for='{r_id}']")
                                            if await lbl.count() > 0:
                                                lbl_text = (await lbl.first.inner_text()).lower().strip()
                                        if norm_v and (norm_v == lbl_text or (len(norm_v) > 3 and norm_v in lbl_text)):
                                            await r_input.check(force=True)
                                            chosen = True
                                            total_filled += 1
                                            await _emit_log(on_log, f"✔ Questão {q_num}: Alternativa selecionada ({lbl_text[:35]}...)")
                                            break
                                if chosen:
                                    break

                    # 2. Checkboxes (Múltiplas respostas)
                    checks = q_el.locator(".answers .answer input[type='checkbox']")
                    c_count = await checks.count()
                    if c_count > 0:
                        target_val = answers_dict.get(q_name) or answers_dict.get(str(q_num)) or ""
                        if target_val:
                            norm_target = target_val.lower().strip()
                            for c_idx in range(c_count):
                                c_input = checks.nth(c_idx)
                                c_id = await c_input.get_attribute("id")
                                lbl_text = ""
                                if c_id:
                                    lbl = q_el.locator(f"#{c_id}_label, label[for='{c_id}']")
                                    if await lbl.count() > 0:
                                        lbl_text = (await lbl.first.inner_text()).lower().strip()
                                if lbl_text and (lbl_text in norm_target or norm_target in lbl_text):
                                    await c_input.check(force=True)
                                    total_filled += 1

                    # 3. Dropdowns / Selects (Questões de Correspondência / Matching)
                    selects = q_el.locator(".answers select, select.question_input")
                    s_count = await selects.count()
                    for s_idx in range(s_count):
                        sel = selects.nth(s_idx)
                        term = await sel.evaluate("el => { const row = el.closest('.answer'); const lbl = row ? row.querySelector('label') : null; return lbl ? lbl.innerText.trim() : ''; }")

                        val_to_select = (
                            answers_dict.get(f"CAMPO_{campo_counter}")
                            or (answers_dict.get(term.upper()) if term else None)
                            or answers_dict.get(f"Q{q_num}_{s_idx+1}")
                        )
                        campo_counter += 1

                        if val_to_select:
                            try:
                                norm_target = str(val_to_select).lower().strip()
                                opts = await sel.evaluate(
                                    "el => Array.from(el.options).map(o => ({val: o.value, text: o.text.trim()}))"
                                )
                                best_val = None
                                for opt in opts:
                                    if not opt.get("val") or "[ choose ]" in opt["text"].lower() or "[ escolha ]" in opt["text"].lower():
                                        continue
                                    norm_opt = opt["text"].lower().strip()
                                    if norm_target == norm_opt or norm_target in norm_opt or norm_opt in norm_target:
                                        best_val = opt["val"]
                                        break
                                if best_val:
                                    await sel.select_option(value=best_val)
                                    total_filled += 1
                                    await _emit_log(on_log, f"✔ Questão {q_num} ({term or f'Item {s_idx+1}'}): Opção associada com sucesso!")
                            except Exception as s_err:
                                console.print(f"[yellow]Aviso ao selecionar opção no Canvas: {s_err}[/yellow]")

                    # 4. Inputs de texto e lacunas (Apenas elementos visíveis e habilitados)
                    text_inps = q_el.locator(".answers input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='submit']), .answers textarea, .question_text input:not([type='hidden']), .question_text textarea")
                    t_count = await text_inps.count()
                    for t_idx in range(t_count):
                        t_input = text_inps.nth(t_idx)
                        try:
                            # Ignora elementos ocultos (ex: textareas de edição interna do professor)
                            if not await t_input.is_visible() or await t_input.is_disabled():
                                continue
                        except Exception:
                            continue

                        val_to_type = (
                            answers_dict.get(f"CAMPO_{campo_counter}")
                            or answers_dict.get(q_name)
                            or answers_dict.get(f"Q{q_num}_{t_idx+1}")
                        )
                        campo_counter += 1

                        if val_to_type:
                            try:
                                await t_input.scroll_into_view_if_needed()
                                await t_input.fill(str(val_to_type))
                                total_filled += 1
                                await asyncio.sleep(0.3)
                            except Exception:
                                pass

                # Se houver botão de 'Next' (One Question at a Time), avança para a próxima questão
                next_btn = page.locator("button.next-question, button#next-question, button[name='next'], a.next-question, button:has-text('Next'), button:has-text('Próxima')").first
                if await next_btn.count() > 0 and await next_btn.is_visible():
                    await _emit_log(on_log, "Avançando para a próxima questão do Canvas...")
                    await next_btn.click()
                    await asyncio.sleep(2.0)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    continue
                else:
                    break

            await _emit_log(on_log, f"📝 Preenchimento finalizado ({total_filled} respostas inseridas).")

            # Aguarda 2 segundos para o Canvas salvar as alterações
            await asyncio.sleep(2.0)

            if not auto_submit:
                # Modo preencher: apenas salva rascunho
                msg = f"Questionário do Canvas preenchido com sucesso ({total_filled} respostas inseridas)! Rascunho salvo para você conferir no portal."
                await _emit_log(on_log, f"✔ {msg}")
                return True, msg

            # Modo finalizar: submete o questionário
            await _emit_log(on_log, "🚀 Submetendo questionário em definitivo no Canvas...")
            
            # Aceita automaticamente diálogos de confirmação de envio do Canvas
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))

            submit_selectors = [
                "#submit_quiz_button",
                "button#submit_quiz_button",
                "input#submit_quiz_button",
                ".submit_quiz_button",
                "button:has-text('Submit Quiz')",
                "button:has-text('Enviar teste')",
                "button:has-text('Submeter teste')",
                "button:has-text('Entregar teste')",
            ]

            submitted = False
            for s_sel in submit_selectors:
                btn = page.locator(s_sel)
                if await btn.count() > 0:
                    try:
                        if await btn.first.is_visible():
                            await btn.first.click()
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                            await asyncio.sleep(2.0)
                            submitted = True
                            break
                    except Exception:
                        continue

            if submitted:
                # Trata modal de confirmação do Canvas se existir (ex: Submit Anyway)
                await asyncio.sleep(1.0)
                confirm_modal_btn = page.locator(
                    ".ui-dialog:visible .ui-dialog-buttonset button, "
                    ".ui-dialog:visible button.btn-primary, "
                    "div[role='dialog']:visible .ui-dialog-buttonset button, "
                    "div[role='dialog']:visible button.btn-primary, "
                    "button:has-text('Submit Anyway'), "
                    "button:has-text('Enviar mesmo assim')"
                )
                if await confirm_modal_btn.count() > 0:
                    try:
                        if await confirm_modal_btn.first.is_visible():
                            await confirm_modal_btn.first.click()
                    except Exception:
                        pass

                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass

                await asyncio.sleep(2.0)

                msg = f"Questionário finalizado e entregue com sucesso no Canvas! ({total_filled} respostas enviadas)"
                await _emit_log(on_log, f"✔ {msg}")
                return True, msg

            return False, "Botão de envio 'Submit Quiz' não encontrado na página do Canvas."

        except Exception as e:
            console.print(f"[red]Erro ao preencher/submeter quiz no Canvas: {e}[/red]")
            await _emit_log(on_log, f"❌ Erro ao preencher quiz no Canvas: {e}")
            return False, f"Erro na automação do Canvas Quiz: {e}"
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if p:
                try:
                    await p.stop()
                except Exception:
                    pass
