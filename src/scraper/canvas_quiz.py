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
            "a.take_quiz_button",
            "a:has-text('Take the Quiz')",
            "a:has-text('Fazer o teste')",
            "a:has-text('Responder ao questionário')",
            "a:has-text('Resume Quiz')",
            "a:has-text('Retomar teste')",
            "a:has-text('Continuar teste')",
            "a:has-text('Take the Quiz Again')",
            "a:has-text('Fazer o teste novamente')",
            "button:has-text('Take the Quiz')",
            "button:has-text('Fazer o teste')",
            "button:has-text('Resume Quiz')",
            "button:has-text('Retomar teste')",
            "button#take_quiz_link",
            ".take_quiz_button",
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

        # Confirma modais de início se houverem
        modal_confirm_selectors = [
            ".ui-dialog button:has-text('Take the Quiz')",
            ".ui-dialog button:has-text('Fazer o teste')",
            ".ui-dialog button:has-text('Iniciar')",
            "div[role='dialog'] button.btn-primary",
        ]
        for m_sel in modal_confirm_selectors:
            m_loc = page.locator(m_sel)
            if await m_loc.count() > 0:
                try:
                    if await m_loc.first.is_visible():
                        await m_loc.first.click()
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    continue

        return "/take" in page.url or await page.locator("#submit_quiz_form, .question, .quiz_sortable").count() > 0

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
                // Canvas classic quizzes usam .question ou .display_question
                let qNodes = Array.from(document.querySelectorAll(".question.display_question, .question, .quiz_sortable .question_holder"));
                if (qNodes.length === 0) {
                    qNodes = Array.from(document.querySelectorAll("div[id^='question_']"));
                }
                
                let globalInputIdx = 1;

                qNodes.forEach((q, qIndex) => {
                    const idAttr = q.getAttribute("id") || `question_${qIndex + 1}`;
                    const qIdMatch = idAttr.match(/question_(\d+)/);
                    const qCanvasId = qIdMatch ? qIdMatch[1] : `${qIndex + 1}`;

                    // Título / Número
                    const nameEl = q.querySelector(".question_name, .name, .header .name");
                    const nameText = nameEl ? nameEl.innerText.trim() : `Questão ${qIndex + 1}`;

                    // Pontos
                    const pointsEl = q.querySelector(".question_points, .points, .header .points");
                    const pointsText = pointsEl ? pointsEl.innerText.trim() : "";

                    // Enunciado e códigos
                    const textEl = q.querySelector(".question_text, .text") || q;
                    const clone = textEl.cloneNode(true);

                    // Limpa elementos de ruído
                    clone.querySelectorAll(".screenreader-only, .accessibility_warning, .answers, .answers_wrapper, button, input[type='submit']").forEach(e => e.remove());

                    // Preserva formatação de código pre/code
                    clone.querySelectorAll("pre, code").forEach(codeBlock => {
                        const codeText = codeBlock.innerText;
                        const marker = `\n\`\`\`\n${codeText}\n\`\`\`\n`;
                        codeBlock.replaceWith(document.createTextNode(marker));
                    });

                    // Inputs de preencher (Fill in the Blank / Short Answer)
                    const inputMap = [];
                    const textInputs = clone.querySelectorAll("input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='submit']), textarea");
                    textInputs.forEach(inp => {
                        const token = `[[CAMPO_${globalInputIdx}]]`;
                        inputMap.push({
                            token: token,
                            key: `CAMPO_${globalInputIdx}`,
                            name: inp.getAttribute("name"),
                            id: inp.getAttribute("id"),
                            index: globalInputIdx,
                            type: inp.tagName.toLowerCase() === "textarea" ? "textarea" : "text"
                        });
                        inp.replaceWith(document.createTextNode(` ${token} `));
                        globalInputIdx++;
                    });

                    // Dropdowns / Selects
                    const selects = clone.querySelectorAll("select");
                    selects.forEach(sel => {
                        const token = `[[CAMPO_${globalInputIdx}]]`;
                        const opts = Array.from(sel.options).map(o => o.text.trim()).filter(t => t && !t.toLowerCase().includes("choose") && !t.toLowerCase().includes("selecion"));
                        inputMap.push({
                            token: token,
                            key: `CAMPO_${globalInputIdx}`,
                            name: sel.getAttribute("name"),
                            id: sel.getAttribute("id"),
                            index: globalInputIdx,
                            type: "select",
                            options: opts
                        });
                        const optsStr = opts.length > 0 ? ` (Opções: ${opts.join(" | ")})` : "";
                        sel.replaceWith(document.createTextNode(` ${token}${optsStr} `));
                        globalInputIdx++;
                    });

                    const cleanQuestionText = clone.innerText.replace(/\n{3,}/g, '\n\n').trim();

                    // Múltipla escolha (Radios)
                    const radioOptions = [];
                    const answerNodes = q.querySelectorAll(".answers .answer, .answer");
                    answerNodes.forEach((ansNode, aIdx) => {
                        const radioInp = ansNode.querySelector("input[type='radio']");
                        if (radioInp) {
                            const labelEl = ansNode.querySelector("label, .answer_label, .answer_text");
                            const optText = labelEl ? labelEl.innerText.trim() : ansNode.innerText.trim();
                            radioOptions.push({
                                id: radioInp.getAttribute("id"),
                                name: radioInp.getAttribute("name"),
                                value: radioInp.getAttribute("value"),
                                text: optText || `Opção ${aIdx + 1}`,
                                checked: radioInp.checked
                            });
                        }
                    });

                    // Caixas de seleção (Checkboxes)
                    const checkboxOptions = [];
                    answerNodes.forEach((ansNode, aIdx) => {
                        const checkInp = ansNode.querySelector("input[type='checkbox']");
                        if (checkInp) {
                            const labelEl = ansNode.querySelector("label, .answer_label, .answer_text");
                            const optText = labelEl ? labelEl.innerText.trim() : ansNode.innerText.trim();
                            checkboxOptions.push({
                                id: checkInp.getAttribute("id"),
                                name: checkInp.getAttribute("name"),
                                value: checkInp.getAttribute("value"),
                                text: optText || `Opção ${aIdx + 1}`,
                                checked: checkInp.checked
                            });
                        }
                    });

                    questions.push({
                        canvas_id: qCanvasId,
                        number: qIndex + 1,
                        name: nameText,
                        points: pointsText,
                        prompt: cleanQuestionText,
                        inputs: inputMap,
                        radio_options: radioOptions,
                        checkbox_options: checkboxOptions,
                        question_type: radioOptions.length > 0 ? "multiple_choice" : (checkboxOptions.length > 0 ? "multiple_answers" : (inputMap.length > 0 ? "fill_in_the_blank" : "text_only"))
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
                return False, "Não foi possível abrir a tentativa de preenchimento do quiz no Canvas."

            await _emit_log(on_log, "Iniciando preenchimento humanizado das questões no Canvas...")

            # Mapeia questões na tela
            q_locators = page.locator(".question.display_question, .question, div[id^='question_']")
            q_count = await q_locators.count()

            total_filled = 0
            for idx in range(q_count):
                q_el = q_locators.nth(idx)
                q_num = idx + 1
                q_name = f"Q{q_num}"

                # 1. Rádios (Múltipla escolha)
                radios = q_el.locator("input[type='radio']")
                r_count = await radios.count()
                if r_count > 0:
                    # Busca resposta correspondente a esta questão
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
                        # Procura por correspondência exata ou parcial de texto na label
                        for r_idx in range(r_count):
                            r_input = radios.nth(r_idx)
                            r_id = await r_input.get_attribute("id")
                            lbl_text = ""
                            if r_id:
                                lbl = q_el.locator(f"label[for='{r_id}']")
                                if await lbl.count() > 0:
                                    lbl_text = (await lbl.first.inner_text()).lower().strip()
                            if not lbl_text:
                                ans_wrap = r_input.locator("..")
                                lbl_text = (await ans_wrap.inner_text()).lower().strip()

                            # Match de texto ou letra (A, B, C, D...)
                            opt_letter = chr(65 + r_idx).lower()  # 'a', 'b', 'c', 'd'
                            if (norm_target == lbl_text
                                or (len(norm_target) > 2 and norm_target in lbl_text)
                                or (len(lbl_text) > 2 and lbl_text in norm_target)
                                or norm_target.startswith(f"{opt_letter})")
                                or norm_target.startswith(f"{opt_letter}.")
                                or norm_target == opt_letter):
                                await r_input.scroll_into_view_if_needed()
                                await asyncio.sleep(0.3)
                                await r_input.check(force=True)
                                chosen = True
                                total_filled += 1
                                await _emit_log(on_log, f"✔ Questão {q_num}: Alternativa selecionada ({lbl_text[:35]}...)")
                                break

                    # Se não encontrou por target_val, tenta buscar por token CAMPO_X
                    if not chosen:
                        for k, v in answers_dict.items():
                            if k.startswith("CAMPO_") and v:
                                norm_v = v.lower().strip()
                                for r_idx in range(r_count):
                                    r_input = radios.nth(r_idx)
                                    r_id = await r_input.get_attribute("id")
                                    lbl_text = ""
                                    if r_id:
                                        lbl = q_el.locator(f"label[for='{r_id}']")
                                        if await lbl.count() > 0:
                                            lbl_text = (await lbl.first.inner_text()).lower().strip()
                                    if norm_v and (norm_v in lbl_text or lbl_text in norm_v):
                                        await r_input.check(force=True)
                                        chosen = True
                                        total_filled += 1
                                        break
                            if chosen:
                                break

                # 2. Checkboxes (Múltiplas respostas)
                checks = q_el.locator("input[type='checkbox']")
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
                                lbl = q_el.locator(f"label[for='{c_id}']")
                                if await lbl.count() > 0:
                                    lbl_text = (await lbl.first.inner_text()).lower().strip()
                            if lbl_text and (lbl_text in norm_target or norm_target in lbl_text):
                                await c_input.check(force=True)
                                total_filled += 1

                # 3. Inputs de texto e lacunas
                text_inps = q_el.locator("input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='submit']), textarea")
                t_count = await text_inps.count()
                for t_idx in range(t_count):
                    t_input = text_inps.nth(t_idx)
                    val_to_type = None
                    # Procura por chaves CAMPO_X ou QX
                    for k, v in answers_dict.items():
                        if k in (f"CAMPO_{total_filled+1}", q_name, f"Q{q_num}_{t_idx+1}"):
                            val_to_type = v
                            break
                    if val_to_type:
                        await t_input.scroll_into_view_if_needed()
                        await t_input.fill(val_to_type)
                        total_filled += 1
                        await asyncio.sleep(0.4)

                # 4. Selects / Comboboxes
                selects = q_el.locator("select")
                s_count = await selects.count()
                for s_idx in range(s_count):
                    sel = selects.nth(s_idx)
                    val_to_select = None
                    for k, v in answers_dict.items():
                        if k in (f"CAMPO_{total_filled+1}", q_name):
                            val_to_select = v
                            break
                    if val_to_select:
                        try:
                            await sel.select_option(label=val_to_select)
                            total_filled += 1
                        except Exception:
                            pass

            await _emit_log(on_log, f"📝 Preenchimento finalizado ({total_filled} respostas inseridas).")

            # Aguarda 2 segundos para o Canvas disparar autosave
            await asyncio.sleep(2.0)

            if not auto_submit:
                # Modo preencher: apenas salva rascunho
                msg = f"Questionário do Canvas preenchido com sucesso ({total_filled} respostas inseridas)! Rascunho salvo para você conferir no portal."
                await _emit_log(on_log, f"✔ {msg}")
                return True, msg

            # Modo finalizar: submete o questionário
            await _emit_log(on_log, "🚀 Submetendo questionário em definitivo no Canvas...")
            submit_selectors = [
                "#submit_quiz_button",
                "button#submit_quiz_button",
                "input#submit_quiz_button",
                "button:has-text('Submit Quiz')",
                "button:has-text('Enviar teste')",
                "button:has-text('Submeter teste')",
                "button:has-text('Entregar teste')",
                ".submit_quiz_button",
            ]

            submitted = False
            for s_sel in submit_selectors:
                btn = page.locator(s_sel)
                if await btn.count() > 0:
                    try:
                        if await btn.first.is_visible():
                            await btn.first.click()
                            submitted = True
                            break
                    except Exception:
                        continue

            if submitted:
                # Trata modal de confirmação do Canvas se existir
                await asyncio.sleep(1.0)
                confirm_modal_btn = page.locator(".ui-dialog button:has-text('Submit'), div[role='dialog'] button:has-text('Submit'), div[role='dialog'] button.btn-primary")
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

                msg = f"Questionário finalizado e entregue com sucesso no Canvas! ({total_filled} respostas)"
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
