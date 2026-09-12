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
import logging
import random
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.async_api import async_playwright
from rich.console import Console

logger = logging.getLogger(__name__)

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


def extract_checkbox_letters(target_text: str) -> Set[str]:
    """Extrai letras de alternativas pretendidas (ex: 'a, c', 'A e D', '[a, c]')
    evitando falsos positivos com preposições e artigos na língua portuguesa (como 'a' em 'a pessoa')."""
    if not target_text:
        return set()
    clean = target_text.strip()
    # 1. Se for uma lista pura de letras ou letras com pontuação (ex: 'a, c', 'A e D', '[a, c]', 'a; b; d')
    letters_only = re.sub(r"[\[\]\(\)\s,;/]+|\s+(?:e|and)\s+", " ", clean, flags=re.IGNORECASE).strip()
    if re.fullmatch(r"(?:[a-eA-E]\s*)+", letters_only):
        return set(re.findall(r"[a-eA-E]", letters_only.lower()))

    # 2. Marcadores explícitos de alternativa: "a.", "a)", "a:", "a -", "- a."
    explicit = set(re.findall(r"(?:^|[\s,;(\[])([a-eA-E])(?:\.|\)|\:|\s*-)", clean.lower()))
    if explicit:
        return explicit

    # 3. Frases como "alternativas A e D", "opções A, B e C", "itens A e C"
    alt_match = re.search(r"(?:alternativas?|opç[õo]es?|itens?)\s+([a-eA-E](?:(?:\s*[,;/]\s*|\s+(?:e|and)\s+)[a-eA-E])*)", clean, re.IGNORECASE)
    if alt_match:
        tokens = re.split(r"\s+(?:e|and)\s+|\s*[,;/]\s*", alt_match.group(1).strip())
        return set(t.lower().strip() for t in tokens if t.lower().strip() in "abcde")

    return set()


def split_association_item(val_str: str) -> Tuple[str, str]:
    """Separa enunciado da linha e valor pretendido em questões de associação.
    Evita que hífens contidos no próprio texto (ex: '1978 - The birth...') sejam confundidos com separadores."""
    clean = re.sub(r"^\s*\d+[\.\)\-]\s*", "", str(val_str)).strip()
    if "→" in clean:
        parts = clean.rsplit("→", 1)
    elif "->" in clean:
        parts = clean.rsplit("->", 1)
    elif ":" in clean:
        parts = clean.rsplit(":", 1)
    else:
        parts = [clean]
    left = re.sub(r"^(?:Q\d+(?:_\d+)?|CAMPO_\d+)\s*:\s*", "", parts[0].strip("* ").strip(), flags=re.IGNORECASE)
    right = parts[1].strip("* ").strip() if len(parts) > 1 else ""
    return left, right


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

    async def _open_or_resume_attempt(self, page, return_to_attempt: bool = True) -> bool:
        """Abre uma nova tentativa, refaz tentativa anterior ('Fazer uma outra tentativa') ou continua tentativa em aberto."""
        if "attempt.php" in page.url:
            return True

        # Se já estiver no resumo da tentativa (summary.php):
        if "summary.php" in page.url:
            if return_to_attempt:
                ret_btn = page.locator("button:has-text('Retornar à tentativa'), a:has-text('Retornar à tentativa'), input[value*='Retornar à tentativa']").first
                if await ret_btn.count() > 0:
                    await ret_btn.click()
                    await page.wait_for_load_state("networkidle")
                    return True
            else:
                # Permanece em summary.php se a intenção for finalizar a tentativa
                return True

        console.print(f"[cyan]Localizando botões de início/retomada de tentativa em: {page.url}[/cyan]")


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
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass
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
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=12000)
                        except Exception:
                            pass
                        await asyncio.sleep(1.2)
                        break
                except Exception:
                    continue

        return "attempt.php" in page.url or "summary.php" in page.url

    async def inspect_and_extract_quiz(self, quiz_url: str, on_log: Optional[Any] = None) -> Dict[str, Any]:
        """Acessa a tentativa do questionário e extrai os enunciados reais com marcadores pontuais."""
        if not self.auth.session_exists:
            console.print(f"[yellow]Aviso: Sessão do Moodle não encontrada em {self.auth.cookies_path}. Pulando inspeção via navegador.[/yellow]")
            await _emit_log(on_log, "Aviso: Sessão local do Moodle não encontrada. Prosseguindo com resolução direta via IA...")
            return {
                "success": False,
                "error": "Sessão do Moodle não encontrada.",
                "questions": []
            }

        p = None
        browser = None
        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(self.auth.cookies_path))
            page = await context.new_page()

            try:
                try:
                    await page.goto(quiz_url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    await page.goto(quiz_url, timeout=25000)

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

                        // Remove todos os spans de acessibilidade, controles internos, botões, barras de ferramentas e containeres de editores
                        clone.querySelectorAll(
                            '.accesshide, .sr-only, .im-controls, .editor_atto_toolbar, .tox, .tox-tinymce, .tox-toolbar, .tox-menubar, .tox-statusbar, .editor_atto_content, input[type="submit"], input[type="button"], button, .comment, .feedback, .grading, .history'
                        ).forEach(el => el.remove());

                        // 1. Inputs de texto, números, textareas e caixas de redação/editor
                        const inputs = clone.querySelectorAll("input:not([type='submit']):not([type='button']):not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']), textarea");
                        const inputMap = [];

                        inputs.forEach(inp => {
                            const token = `[[CAMPO_${globalInputIdx}]]`;
                            inputMap.push({
                                token: token,
                                key: `CAMPO_${globalInputIdx}`,
                                name: inp.getAttribute("name"),
                                id: inp.getAttribute("id"),
                                index: globalInputIdx,
                                type: inp.tagName.toLowerCase() === "textarea" ? "textarea" : (inp.getAttribute("type") || "text")
                            });
                            const textNode = document.createTextNode(` ${token} `);
                            inp.parentNode.replaceChild(textNode, inp);
                            globalInputIdx++;
                        });

                        // 2. Lacunas de Arrastar e Soltar (Drag and Drop into text - ddwtos)
                        const dropZones = clone.querySelectorAll(".dropzone, span.drop, [class*='drop'][class*='place']");
                        dropZones.forEach((dz, dzIdx) => {
                            const token = `[[CAMPO_${globalInputIdx}]]`;
                            inputMap.push({
                                token: token,
                                key: `CAMPO_${globalInputIdx}`,
                                name: dz.getAttribute("name") || `dd_${dzIdx+1}`,
                                id: dz.getAttribute("id") || `dd_${dzIdx+1}`,
                                index: globalInputIdx,
                                type: "dragdrop"
                            });
                            const textNode = document.createTextNode(` ${token} `);
                            dz.parentNode.replaceChild(textNode, dz);
                            globalInputIdx++;
                        });

                        // 3. Menus suspensos / Comboboxes (select)
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

                        // 4. Opções de rádio e checkbox (múltipla escolha)
                        const radioOptions = [];
                        const checkboxOptions = [];
                        const answerNode = q.querySelector(".answer") || q;
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

                            const checkboxes = answerNode.querySelectorAll("input[type='checkbox']");
                            checkboxes.forEach(cb => {
                                const lbl = cb.closest("div, label, tr");
                                checkboxOptions.push({
                                    name: cb.getAttribute("name"),
                                    value: cb.getAttribute("value"),
                                    text: lbl ? lbl.innerText.trim() : ""
                                });
                            });
                        }

                        // Palavras arrastáveis disponíveis no enunciado (ddwtos)
                        const dragHomes = Array.from(q.querySelectorAll(".draghome, .drag")).map(d => d.innerText.trim()).filter(Boolean);

                        // Limpeza de resíduos de texto do Moodle
                        let cleanText = clone.innerText.trim();
                        cleanText = cleanText.replace(/Texto (?:informativo|da questão)/gi, '');
                        cleanText = cleanText.replace(/Resposta \d+\s*Questão \d+/gi, '');
                        cleanText = cleanText.replace(/Verificar Questão \d+/gi, '');
                        cleanText = cleanText.replace(/Questão \d+\s*Anot/gi, 'Anot');
                        cleanText = cleanText.replace(/\n{3,}/g, '\n\n').trim();

                        if (checkboxOptions.length > 0) {
                            cleanText += "\n(Atenção: Questão de múltipla escolha com caixas de seleção / checkboxes. Mais de uma alternativa pode estar correta. Indique todas as opções corretas.)";
                        }
                        if (dragHomes.length > 0) {
                            const uniqueDrags = [...new Set(dragHomes)];
                            cleanText += `\n(Palavras disponíveis para arrastar: ${uniqueDrags.join(" | ")})`;
                        }

                        // Se for apenas bloco informativo do Moodle (.que.description) sem campos a responder
                        const isInfoOnly = q.classList.contains("description") || (inputMap.length === 0 && radioOptions.length === 0 && checkboxOptions.length === 0 && !cleanText.includes("?"));

                        questions.push({
                            qIndex: qIndex + 1,
                            qNumberText: noText,
                            fullTextWithTokens: cleanText,
                            inputsCount: inputMap.length,
                            inputs: inputMap,
                            radios: radioOptions,
                            checkboxes: checkboxOptions,
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
        except Exception as e:
            console.print(f"[yellow]Aviso: Falha ao inicializar Playwright para quiz ({e}). Prosseguindo com resolução direta via IA...[/yellow]")
            await _emit_log(on_log, f"Aviso ao inicializar navegador ({e}). Prosseguindo com resolução direta via IA...")
            return {
                "success": False,
                "error": str(e),
                "questions": []
            }

    async def _verify_all_questions_on_current_attempt(self, page, on_log: Optional[Any] = None) -> int:
        """Clica no botão 'Verificar' de cada questão que ainda não foi validada, no máximo 1 vez por questão."""
        if "attempt.php" not in page.url:
            return 0

        verified_count = 0
        attempted_buttons: set[str] = set()
        attempted_qids: set[str] = set()
        max_clicks = 25
        clicks = 0

        while clicks < max_clicks and "attempt.php" in page.url:
            # Avalia no DOM quais questões ainda não foram validadas
            pending_questions = await page.evaluate(r'''() => {
                const results = [];
                const qNodes = document.querySelectorAll(".que");
                
                qNodes.forEach((q, idx) => {
                    const qId = q.id || `q_${idx+1}`;
                    
                    // 1. Verifica se a questão já possui feedback, nota ou estado concluído
                    const stateEl = q.querySelector(".info .state");
                    const stateText = stateEl ? stateEl.innerText.trim().toLowerCase() : "";
                    
                    const isGraded = stateText.includes("correto") || 
                                     stateText.includes("correta") || 
                                     stateText.includes("correct") || 
                                     stateText.includes("incorreto") || 
                                     stateText.includes("incorreta") || 
                                     stateText.includes("incorrect") || 
                                     stateText.includes("atingiu") || 
                                     stateText.includes("mark") || 
                                     stateText.includes("pontu");

                    // Feedback, outcome ou ícones de acerto/erro (checkmarks fa-check, text-success, etc.)
                    const hasOutcome = !!q.querySelector(".outcome, .feedback, .grading, .history, .comment, .specificfeedback");
                    const hasCheckmarks = !!q.querySelector(".fa-check, .text-success, .correct, .incorrect, .feedbackimage, [title*='Correto'], [alt*='Correto'], [title*='Correct'], [alt*='Correct'], i.fa-check");
                    const hasClassGraded = q.classList.contains("correct") || 
                                           q.classList.contains("incorrect") ||
                                           q.classList.contains("partiallycorrect");

                    // 2. Localiza o botão 'Verificar' desta questão específica de forma compatível com DOM puro
                    const buttons = Array.from(q.querySelectorAll("input[type='submit'], button[type='submit'], button.submit, input.submit, button"));
                    let verifyBtn = null;
                    for (const b of buttons) {
                        const text = (b.innerText || b.value || "").trim().toLowerCase();
                        const name = (b.getAttribute("name") || "").toLowerCase();
                        const isVerify = text.includes("verificar") || text.includes("check") || name.endsWith("-submit");
                        const isTryAgain = name.includes("-tryagain") || text.includes("tentar novamente") || text.includes("try again");
                        
                        if (isVerify && !isTryAgain) {
                            verifyBtn = b;
                            break;
                        }
                    }

                    const btnName = verifyBtn ? (verifyBtn.getAttribute("name") || "") : "";
                    const isBtnDisabled = verifyBtn ? (
                        verifyBtn.disabled || 
                        verifyBtn.classList.contains("disabled") || 
                        verifyBtn.getAttribute("aria-disabled") === "true"
                    ) : true;
                    
                    const isVisible = verifyBtn ? (
                        verifyBtn.offsetWidth > 0 && 
                        verifyBtn.offsetHeight > 0 && 
                        window.getComputedStyle(verifyBtn).visibility !== 'hidden' &&
                        window.getComputedStyle(verifyBtn).display !== 'none'
                    ) : false;

                    const hasActiveVerifyBtn = !!verifyBtn && isVisible && !isBtnDisabled;

                    // Se a questão possui botão 'Verificar' ativo e visível, ela OBRIGATORIAMENTE NÃO está verificada!
                    // Só é considerada já verificada se tiver feedback/nota e NÃO possuir botão de verificação pendente.
                    const alreadyVerified = !hasActiveVerifyBtn && (isGraded || hasOutcome || hasCheckmarks || hasClassGraded);

                    results.push({
                        qId: qId,
                        btnName: btnName,
                        hasBtn: hasActiveVerifyBtn,
                        alreadyVerified: alreadyVerified,
                        stateText: stateText
                    });
                });
                return results;
            }''')

            # Encontra a próxima questão elegível para verificação
            candidate = None
            for q_info in pending_questions:
                btn_name = q_info.get("btnName")
                q_id = q_info.get("qId")
                already_verified = q_info.get("alreadyVerified")
                has_btn = q_info.get("hasBtn")

                if already_verified:
                    continue
                if not has_btn or not btn_name:
                    continue
                if btn_name in attempted_buttons or q_id in attempted_qids:
                    continue

                # Extrai prefixo da questão (ex: 'q78308' de 'q78308:1_-submit')
                q_prefix = btn_name.split(":")[0] if ":" in btn_name else ""
                if q_prefix and q_prefix in attempted_qids:
                    continue

                candidate = q_info
                break

            if not candidate:
                # Nenhuma questão elegível restante para verificação nesta página
                break

            btn_name = candidate["btnName"]
            q_id = candidate["qId"]
            q_prefix = btn_name.split(":")[0] if ":" in btn_name else ""

            attempted_buttons.add(btn_name)
            attempted_qids.add(q_id)
            if q_prefix:
                attempted_qids.add(q_prefix)

            console.print(f"  ✔ [Verificar] Acionando botão da questão ({btn_name})...")
            await _emit_log(on_log, f"Acionando botão 'Verificar' da questão ({btn_name})...")

            try:
                btn_loc = page.locator(f"[name='{btn_name}']").first
                if await btn_loc.count() > 0 and await btn_loc.is_visible():
                    await btn_loc.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.4, 0.7))
                    await btn_loc.click()
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=12000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1000)
                    verified_count += 1
                    clicks += 1
                else:
                    break
            except Exception as click_err:
                console.print(f"  [yellow]Nota ao clicar em Verificar ({btn_name}): {click_err}[/yellow]")
                break

        if verified_count > 0:
            console.print(f"[green]✔ Total de questões verificadas no Moodle nesta etapa: {verified_count}[/green]")
            await _emit_log(on_log, f"✔ {verified_count} questão(ões) verificada(s) com sucesso no Moodle.")
        else:
            console.print("[dim]Nenhum botão de verificação pendente (questionário em modo de feedback diferido ou já validado).[/dim]")
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
                if isinstance(v, list):
                    answers_dict[clean_k] = ", ".join(str(x) for x in v)
                    for s_idx, sv in enumerate(v):
                        answers_dict[f"{clean_k}_{s_idx+1}"] = str(sv)
                else:
                    answers_dict[clean_k] = str(v)

                q_m = re.search(r"^(?:QUEST[ÃA]O|Q)\s*(\d+)(?:_(\d+))?$", clean_k)
                if q_m:
                    num = q_m.group(1)
                    sub = q_m.group(2)
                    if sub:
                        answers_dict[f"Q{num}_{sub}"] = answers_dict[clean_k]
                    else:
                        answers_dict[f"Q{num}"] = answers_dict[clean_k]
                elif clean_k.isdigit():
                    answers_dict[f"Q{clean_k}"] = answers_dict[clean_k]
        elif isinstance(answers, list):
            for item in answers:
                if isinstance(item, dict):
                    k = item.get("key") or item.get("field")
                    v = item.get("value")
                    if k is not None and v is not None:
                        clean_k = str(k).upper().replace("[[", "").replace("]]", "").strip()
                        if isinstance(v, list):
                            answers_dict[clean_k] = ", ".join(str(x) for x in v)
                            for s_idx, sv in enumerate(v):
                                answers_dict[f"{clean_k}_{s_idx+1}"] = str(sv)
                        else:
                            answers_dict[clean_k] = str(v)
                        q_m = re.search(r"^(?:QUEST[ÃA]O|Q)\s*(\d+)(?:_(\d+))?$", clean_k)
                        if q_m:
                            num = q_m.group(1)
                            sub = q_m.group(2)
                            if sub:
                                answers_dict[f"Q{num}_{sub}"] = answers_dict[clean_k]
                            else:
                                answers_dict[f"Q{num}"] = answers_dict[clean_k]
                        elif clean_k.isdigit():
                            answers_dict[f"Q{clean_k}"] = answers_dict[clean_k]
                    elif "question" in item:
                        q_num = item["question"]
                        ans_val = item.get("answer") or item.get("answers")
                        if isinstance(ans_val, list):
                            joined = ", ".join(str(sv) for sv in ans_val)
                            answers_dict[f"Q{q_num}"] = joined
                            for s_idx, sv in enumerate(ans_val):
                                answers_dict[f"Q{q_num}_{s_idx+1}"] = str(sv)
                        else:
                            answers_dict[f"Q{q_num}"] = str(ans_val)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=self.auth.cookies_path)
            page = await context.new_page()

            try:
                try:
                    await page.goto(quiz_url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    await page.goto(quiz_url, timeout=25000)

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

                        # 1. Inputs de texto, numéricos e caixas de redação/editores ricos
                        text_inputs = q_el.locator("input:not([type='submit']):not([type='button']):not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']), textarea")
                        text_count = await text_inputs.count()

                        for t_idx in range(text_count):
                            inp = text_inputs.nth(t_idx)
                            token_key = f"CAMPO_{global_input_idx}"
                            
                            # Busca a resposta correspondente (com suporte amplo a aliases de chaves)
                            if text_count == 1:
                                # Questão com campo único de resposta: prioriza sempre a chave direta da questão (Q3, QUESTÃO 3, etc.)
                                target_val = (
                                    answers_dict.get(f"Q{q_num}") or
                                    answers_dict.get(f"QUESTAO_{q_num}") or
                                    answers_dict.get(f"QUESTAO {q_num}") or
                                    answers_dict.get(f"Questão {q_num}") or
                                    answers_dict.get(str(q_num)) or
                                    answers_dict.get(f"Q{q_num}_{t_idx+1}") or
                                    answers_dict.get(token_key)
                                )
                            else:
                                # Questão com múltiplas lacunas no texto: prioriza a lacuna específica ou token
                                target_val = (
                                    answers_dict.get(f"Q{q_num}_{t_idx+1}") or
                                    answers_dict.get(token_key) or
                                    answers_dict.get(f"Q{q_num}") or
                                    answers_dict.get(f"QUESTAO_{q_num}") or
                                    answers_dict.get(f"QUESTAO {q_num}") or
                                    answers_dict.get(f"Questão {q_num}") or
                                    answers_dict.get(str(q_num))
                                )

                            # Limpa barras e escolhe apenas a primeira opção se vier com barra (apenas para lacunas curtas)
                            if target_val and "/" in str(target_val) and len(str(target_val)) < 60:
                                options = [o.strip() for o in str(target_val).split("/") if o.strip()]
                                target_val = options[0] if options else target_val

                            if target_val:
                                target_val = str(target_val).strip()

                                # Limpa eventuais marcadores como "**Resposta:**" ou "- **Resposta:**" e asteriscos
                                target_val = re.sub(r"^\s*-\s*\*\*Resposta:\*\*\s*", "", target_val, flags=re.IGNORECASE).strip()
                                target_val = re.sub(r"^\s*\*\*Resposta:\*\*\s*", "", target_val, flags=re.IGNORECASE).strip()
                                target_val = re.sub(r"^\s*Resposta:\s*", "", target_val, flags=re.IGNORECASE).strip()
                                target_val = target_val.strip("*").strip()

                                is_visible = False
                                try:
                                    is_visible = await inp.is_visible()
                                except Exception:
                                    is_visible = False

                                inp_id = ""
                                try:
                                    inp_id = (await inp.get_attribute("id")) or ""
                                except Exception:
                                    pass

                                field_type = ""
                                try:
                                    field_type = (await inp.get_attribute("data-fieldtype")) or ""
                                except Exception:
                                    pass

                                # Trata editores ricos do Moodle (TinyMCE 6 / TinyMCE 5 / Atto / Questões discursivas)
                                is_tinymce_present = (await q_el.locator(".tox, .tox-tinymce, iframe.tox-edit-area__iframe, iframe[id$='_ifr']").count() > 0)
                                inp_tag = ""
                                try:
                                    inp_tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                                except Exception:
                                    pass
                                is_rich_editor = (field_type == "editor") or is_tinymce_present or (inp_tag == "textarea") or (not is_visible and "answer" in inp_id)

                                if is_rich_editor:
                                    console.print(f"  [cyan]Detectado editor rico/discursivo no Moodle para [{token_key}][/cyan]")
                                    await _emit_log(on_log, f"Preenchendo resposta dissertativa para [{token_key}]...")

                                    visual_typed = False

                                    # A. TinyMCE 6 / TinyMCE 5 (Moodle 4.x) via API JavaScript oficial do navegador
                                    try:
                                        tinymce_updated = await page.evaluate(r"""([inpId, val]) => {
                                            if (!window.tinymce) return false;
                                            let ed = window.tinymce.get(inpId);
                                            if (!ed && window.tinymce.editors) {
                                                for (let i = 0; i < window.tinymce.editors.length; i++) {
                                                    const item = window.tinymce.editors[i];
                                                    if (item.id === inpId || item.id === inpId + '_ifr' || 
                                                        (item.targetElm && (item.targetElm.id === inpId || item.targetElm.name === inpId || item.targetElm.id.includes(inpId)))) {
                                                        ed = item;
                                                        break;
                                                    }
                                                }
                                                if (!ed && window.tinymce.editors.length === 1) {
                                                    ed = window.tinymce.editors[0];
                                                }
                                            }
                                            if (!ed && window.tinymce.activeEditor) {
                                                ed = window.tinymce.activeEditor;
                                            }
                                            if (ed) {
                                                const formatted = val.includes('<p>') ? val : '<p>' + val.replace(/\n/g, '<br>') + '</p>';
                                                ed.setContent(formatted);
                                                if (typeof ed.save === 'function') ed.save();
                                                ed.fire('change');
                                                ed.fire('input');
                                                return true;
                                            }
                                            return false;
                                        }""", [inp_id, target_val])
                                        if tinymce_updated:
                                            visual_typed = True
                                    except Exception as tiny_js_err:
                                        logger.debug(f"TinyMCE JS API check: {tiny_js_err}")

                                    # B. Se não atualizou via JS API (ex: AMD module), usa Iframe do Playwright diretamente
                                    if not visual_typed:
                                        tinymce_frames = [
                                            q_el.frame_locator("iframe.tox-edit-area__iframe, iframe[id$='_ifr'], .tox iframe, iframe"),
                                            page.frame_locator(f"iframe[id*='{inp_id}'], iframe.tox-edit-area__iframe") if inp_id else q_el.frame_locator("iframe")
                                        ]
                                        for fr in tinymce_frames:
                                            try:
                                                body_loc = fr.locator("body#tinymce, body.mce-content-body, body[contenteditable='true']").first
                                                if await body_loc.count() > 0:
                                                    await body_loc.focus()
                                                    await body_loc.evaluate("(el) => { el.innerHTML = ''; }")
                                                    if len(target_val) <= 250:
                                                        await body_loc.press_sequentially(target_val, delay=random.randint(20, 50))
                                                    else:
                                                        await body_loc.evaluate(r"""(el, val) => {
                                                            const p = document.createElement('p');
                                                            p.textContent = val;
                                                            el.innerHTML = '';
                                                            el.appendChild(p);
                                                            el.dispatchEvent(new Event('input', { bubbles: true }));
                                                            el.dispatchEvent(new Event('change', { bubbles: true }));
                                                        }""", target_val)
                                                        await body_loc.press("End")
                                                    visual_typed = True
                                                    await body_loc.dispatch_event("input")
                                                    await body_loc.dispatch_event("change")
                                                    break
                                            except Exception as fr_err:
                                                logger.debug(f"Tentativa de digitação no iframe TinyMCE: {fr_err}")

                                    # Sincroniza TinyMCE via save() se disponível
                                    try:
                                        await page.evaluate(r"""([inpId]) => {
                                            if (!window.tinymce) return;
                                            let ed = window.tinymce.get(inpId);
                                            if (!ed && window.tinymce.editors && window.tinymce.editors.length > 0) ed = window.tinymce.editors[0];
                                            if (ed && typeof ed.save === 'function') ed.save();
                                        }""", [inp_id])
                                    except Exception:
                                        pass

                                    # C. Atto Editor (Moodle 3.x) ou DIVs com contenteditable="true"
                                    if not visual_typed:
                                        editor_locators = []
                                        if inp_id:
                                            editor_locators.append(q_el.locator(f"[id='{inp_id}editable']"))
                                        editor_locators.extend([
                                            q_el.locator("div.editor_atto_content"),
                                            q_el.locator("div[contenteditable='true']"),
                                            q_el.locator(".form-editor div[role='textbox']")
                                        ])

                                        for ed_loc in editor_locators:
                                            try:
                                                if await ed_loc.count() > 0 and await ed_loc.first.is_visible():
                                                    target_ed = ed_loc.first
                                                    await target_ed.focus()
                                                    await target_ed.evaluate("(el) => { el.innerHTML = ''; }")
                                                    if len(target_val) <= 250:
                                                        await target_ed.press_sequentially(target_val, delay=random.randint(20, 50))
                                                    else:
                                                        await target_ed.evaluate(r"""(el, val) => {
                                                            const p = document.createElement('p');
                                                            p.textContent = val;
                                                            el.innerHTML = '';
                                                            el.appendChild(p);
                                                            el.dispatchEvent(new Event('input', { bubbles: true }));
                                                            el.dispatchEvent(new Event('change', { bubbles: true }));
                                                        }""", target_val)
                                                        await target_ed.press("End")
                                                    visual_typed = True
                                                    break
                                            except Exception as ed_err:
                                                logger.debug(f"Foco no editor visual falhou: {ed_err}")

                                    # D. Injeção direta no <textarea> subjacente (garante envio no POST do Moodle)
                                    try:
                                        await inp.evaluate(r"""(el, val) => {
                                            if (!el.value || el.value.trim() === '') {
                                                el.value = val;
                                            }
                                            el.dispatchEvent(new Event('input', { bubbles: true }));
                                            el.dispatchEvent(new Event('change', { bubbles: true }));
                                        }""", target_val)
                                    except Exception as tx_err:
                                        logger.warning(f"Falha ao injetar valor no textarea: {tx_err}")

                                    total_filled += 1
                                    console.print(f"  ✔ [{token_key}] Preenchido editor rico (TinyMCE/Atto) com sucesso!")
                                    await _emit_log(on_log, f"✔ [{token_key}] Preenchido (Dissertativa): '{target_val[:30]}...'")
                                    await asyncio.sleep(random.uniform(1.0, 2.5))

                                elif is_visible:
                                    # Campo visível padrão (lacuna de texto, número, etc.)
                                    try:
                                        await inp.focus()
                                        await inp.fill("") # Limpa valor anterior
                                        await asyncio.sleep(random.uniform(0.2, 0.4))
                                        await inp.press_sequentially(target_val, delay=random.randint(35, 75))
                                        total_filled += 1
                                        console.print(f"  ✔ [{token_key}] Preenchido com cadência humana: '{target_val}'")
                                        await _emit_log(on_log, f"✔ [{token_key}] Preenchido: '{target_val[:25]}'")
                                        await asyncio.sleep(random.uniform(0.8, 2.0))
                                    except Exception as fill_err:
                                        console.print(f"  [yellow]Aviso: fallback para evaluate em [{token_key}] ({fill_err})[/yellow]")
                                        await inp.evaluate(r"""(el, val) => {
                                            el.value = val;
                                            el.dispatchEvent(new Event('input', { bubbles: true }));
                                            el.dispatchEvent(new Event('change', { bubbles: true }));
                                        }""", target_val)
                                        total_filled += 1
                                else:
                                    # Campo oculto não-editor (fallback seguro sem Playwright timeout)
                                    console.print(f"  [cyan]Campo [{token_key}] oculto; aplicando valor diretamente via DOM[/cyan]")
                                    await inp.evaluate(r"""(el, val) => {
                                        el.value = val;
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                    }""", target_val)
                                    total_filled += 1

                            global_input_idx += 1

                        # Se não encontrou nenhum input/textarea tradicional mas há editor rico isolado (TinyMCE / Atto) na questão
                        if text_count == 0:
                            has_isolated_editor = (await q_el.locator(".tox, .tox-tinymce, iframe.tox-edit-area__iframe, .editor_atto_content, div[contenteditable='true']").count() > 0)
                            if has_isolated_editor:
                                token_key = f"CAMPO_{global_input_idx}"
                                target_val = (
                                    answers_dict.get(f"Q{q_num}") or
                                    answers_dict.get(f"QUESTAO_{q_num}") or
                                    answers_dict.get(f"QUESTAO {q_num}") or
                                    answers_dict.get(f"Questão {q_num}") or
                                    answers_dict.get(str(q_num)) or
                                    answers_dict.get(token_key)
                                )
                                if target_val:
                                    console.print(f"  [cyan]Detectado editor rico isolado sem textarea visível para [{token_key}][/cyan]")
                                    await _emit_log(on_log, f"Preenchendo editor dissertativo para [{token_key}]...")
                                    filled_isolated = False
                                    try:
                                        filled_isolated = await page.evaluate(r"""(val) => {
                                            if (!window.tinymce || !window.tinymce.editors || window.tinymce.editors.length === 0) return false;
                                            const ed = window.tinymce.activeEditor || window.tinymce.editors[0];
                                            if (ed) {
                                                const formatted = val.includes('<p>') ? val : '<p>' + val.replace(/\n/g, '<br>') + '</p>';
                                                ed.setContent(formatted);
                                                if (typeof ed.save === 'function') ed.save();
                                                ed.fire('change');
                                                ed.fire('input');
                                                return true;
                                            }
                                            return false;
                                        }""", str(target_val))
                                    except Exception:
                                        pass
                                    if not filled_isolated:
                                        try:
                                            fr = q_el.frame_locator("iframe.tox-edit-area__iframe, iframe[id$='_ifr'], .tox iframe, iframe")
                                            body_loc = fr.locator("body#tinymce, body.mce-content-body, body[contenteditable='true']").first
                                            if await body_loc.count() > 0:
                                                await body_loc.focus()
                                                await body_loc.evaluate("(el) => { el.innerHTML = ''; }")
                                                if len(str(target_val)) <= 250:
                                                    await body_loc.press_sequentially(str(target_val), delay=random.randint(20, 50))
                                                else:
                                                    await body_loc.evaluate(r"""(el, val) => {
                                                        const p = document.createElement('p');
                                                        p.textContent = val;
                                                        el.innerHTML = '';
                                                        el.appendChild(p);
                                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                                    }""", str(target_val))
                                                filled_isolated = True
                                        except Exception:
                                            pass
                                    if filled_isolated:
                                        total_filled += 1
                                        console.print(f"  ✔ [{token_key}] Editor isolado preenchido com sucesso!")
                                        await _emit_log(on_log, f"✔ [{token_key}] Preenchido (Dissertativa): '{str(target_val)[:30]}...'")
                                global_input_idx += 1

                        # 2. Alternativas de Múltipla Escolha (Radio buttons) - Suporta escolha única e matrizes (V/F por linha)
                        radios = q_el.locator("input[type='radio']")
                        r_count = await radios.count()
                        if r_count > 0:
                            # Identifica grupos de radio pelo atributo 'name' (suporta matrizes e tabelas de Verdadeiro/Falso)
                            radio_names = []
                            for r_idx in range(r_count):
                                r_name = await radios.nth(r_idx).get_attribute("name") or "default"
                                if r_name not in radio_names:
                                    radio_names.append(r_name)

                            for g_idx, group_name in enumerate(radio_names):
                                group_radios = q_el.locator(f"input[type='radio'][name='{group_name}']")
                                gr_count = await group_radios.count()
                                if gr_count == 0:
                                    continue

                                # Rótulo textual da linha/pergunta associada (para matrizes)
                                row_label = await group_radios.first.evaluate('''el => {
                                    const row = el.closest('tr, .form-inline, .row, div.d-flex');
                                    if (!row) return '';
                                    const clone = row.cloneNode(true);
                                    clone.querySelectorAll('input, .accesshide, .sr-only').forEach(e => e.remove());
                                    return clone.innerText.trim();
                                }''')

                                if len(radio_names) == 1:
                                    target_val = (
                                        answers_dict.get(f"Q{q_num}") or
                                        answers_dict.get(f"QUESTAO_{q_num}") or
                                        answers_dict.get(f"QUESTAO {q_num}") or
                                        answers_dict.get(f"Questão {q_num}") or
                                        answers_dict.get(str(q_num)) or
                                        answers_dict.get(f"CAMPO_{global_input_idx}")
                                    )
                                else:
                                    target_val = (
                                        answers_dict.get(f"Q{q_num}_{g_idx+1}") or
                                        answers_dict.get(f"Q{q_num}_{chr(97+g_idx)}") or
                                        answers_dict.get(f"CAMPO_{global_input_idx}")
                                    )
                                    if not target_val and row_label:
                                        norm_row = normalize_str(row_label)
                                        for k, v in answers_dict.items():
                                            parts = re.split(r"[→\->:]", str(v), maxsplit=1)
                                            if len(parts) == 2 and (norm_row in normalize_str(parts[0]) or normalize_str(parts[0]) in norm_row):
                                                target_val = parts[1].strip()
                                                break

                                if target_val:
                                    clean_target = str(target_val).strip()
                                    clean_target = re.sub(r"^\s*-\s*\*\*Resposta:\*\*\s*", "", clean_target, flags=re.IGNORECASE).strip()
                                    clean_target = re.sub(r"^\s*\*\*Resposta:\*\*\s*", "", clean_target, flags=re.IGNORECASE).strip()
                                    clean_target = re.sub(r"^\s*Resposta:\s*", "", clean_target, flags=re.IGNORECASE).strip()

                                    target_letter_match = re.match(r"^\s*([a-eA-E])(?:\.|\)|\:|\s|$)", clean_target)
                                    target_letter = target_letter_match.group(1).lower() if target_letter_match else None
                                    target_content = re.sub(r"^\s*([a-eA-E])(?:\.|\)|\:|\s|-)+", "", clean_target).strip()
                                    norm_target_content = normalize_str(target_content)

                                    candidate_options = []
                                    for r_idx in range(gr_count):
                                        r_inp = group_radios.nth(r_idx)
                                        r_id = await r_inp.get_attribute("id")
                                        opt_raw_text = ""
                                        if r_id:
                                            lbl_loc = q_el.locator(f"label[for='{r_id}']")
                                            if await lbl_loc.count() > 0:
                                                opt_raw_text = await lbl_loc.first.inner_text()
                                        if not opt_raw_text.strip():
                                            opt_raw_text = await r_inp.evaluate("""el => {
                                                const lbl = el.closest('label');
                                                if (lbl && lbl.innerText && lbl.innerText.trim()) return lbl.innerText;
                                                const container = el.closest('.r0, .r1, .answer, [class*="answer"], div.d-flex');
                                                if (container && container.innerText && container.innerText.trim()) return container.innerText;
                                                return '';
                                            }""") or ""

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

                                    best_match = None
                                    best_score = -1
                                    compact_target = re.sub(r"[\s\-_,;]+", "", norm_target_content)
                                    for opt in candidate_options:
                                        score = 0
                                        compact_opt = re.sub(r"[\s\-_,;]+", "", opt["norm_content"])

                                        if len(norm_target_content) >= 3 and len(opt["norm_content"]) >= 3:
                                            if norm_target_content == opt["norm_content"]:
                                                score = 100
                                            elif compact_target and compact_target == compact_opt:
                                                score = 100
                                            elif norm_target_content in opt["norm_content"] and len(norm_target_content) >= 8:
                                                score = 90
                                            elif opt["norm_content"] in norm_target_content and len(opt["norm_content"]) >= 8:
                                                score = 90
                                            elif compact_target and compact_opt and len(compact_target) >= 6 and (compact_target in compact_opt or compact_opt in compact_target):
                                                score = 90
                                            else:
                                                words_target = set(w for w in norm_target_content.split() if len(w) > 2)
                                                words_opt = set(w for w in opt["norm_content"].split() if len(w) > 2)
                                                if words_target and words_opt:
                                                    overlap = len(words_target & words_opt)
                                                    jaccard = overlap / max(len(words_target), len(words_opt))
                                                    if jaccard >= 0.4:
                                                        score = 50 + int(jaccard * 35)

                                        if target_letter and opt["letter"] and target_letter == opt["letter"]:
                                            if len(norm_target_content) < 3:
                                                score = 80
                                            else:
                                                score += 15

                                        if score > best_score:
                                            best_score = score
                                            best_match = opt

                                    if best_match and best_score >= 40:
                                        await asyncio.sleep(random.uniform(0.6, 1.5))
                                        try:
                                            await best_match["locator"].check(timeout=4000)
                                        except Exception:
                                            await best_match["locator"].evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); el.dispatchEvent(new Event('input', { bubbles: true })); }")
                                        total_filled += 1
                                        tag_str = f"Q{q_num}.{g_idx+1}" if len(radio_names) > 1 else f"Q{q_num}"
                                        console.print(
                                            f"  ✔ [{tag_str}] Alternativa marcada (confiança {best_score}%): "
                                            f"[{best_match['letter'] or '?'}] {best_match['content'][:40]}"
                                        )
                                        await _emit_log(on_log, f"✔ [{tag_str}] Marcada: [{best_match['letter'] or '?'}] {best_match['content'][:30]}")

                        # 3. Alternativas de Múltipla Escolha (Checkboxes - "Selecione uma ou mais alternativas")
                        checkboxes = q_el.locator("input[type='checkbox']")
                        cb_count = await checkboxes.count()
                        if cb_count > 0:
                            raw_cb_target = (
                                answers_dict.get(f"Q{q_num}") or
                                answers_dict.get(f"QUESTAO_{q_num}") or
                                answers_dict.get(f"QUESTAO {q_num}") or
                                answers_dict.get(f"Questão {q_num}") or
                                answers_dict.get(str(q_num)) or ""
                            )
                            if not raw_cb_target:
                                sub_vals = [answers_dict[k] for k in answers_dict if k.startswith(f"Q{q_num}_")]
                                if sub_vals:
                                    raw_cb_target = " ".join(sub_vals)
                            if raw_cb_target:
                                target_cb_str = str(raw_cb_target)
                                mentioned_letters = extract_checkbox_letters(target_cb_str)
                                norm_target_cb = normalize_str(target_cb_str)

                                for cb_idx in range(cb_count):
                                    cb_inp = checkboxes.nth(cb_idx)
                                    cb_id = await cb_inp.get_attribute("id")
                                    cb_raw_text = ""
                                    if cb_id:
                                        lbl_loc = q_el.locator(f"label[for='{cb_id}']")
                                        if await lbl_loc.count() > 0:
                                            cb_raw_text = await lbl_loc.first.inner_text()
                                    if not cb_raw_text.strip():
                                        cb_raw_text = await cb_inp.evaluate("""el => {
                                            const lbl = el.closest('label');
                                            if (lbl && lbl.innerText && lbl.innerText.trim()) return lbl.innerText;
                                            const container = el.closest('.r0, .r1, .answer, [class*="answer"], div.d-flex');
                                            if (container && container.innerText && container.innerText.trim()) return container.innerText;
                                            return '';
                                        }""") or ""

                                    clean_cb = cb_raw_text.replace("\n", " ").strip()
                                    clean_cb = re.sub(r"(?i)não respondido|marcado|selecionado", "", clean_cb).strip()
                                    cb_letter_m = re.search(r"(?:^|\s)([a-eA-E])(?:\.|\)|\:)\s*", clean_cb)
                                    cb_letter = cb_letter_m.group(1).lower() if cb_letter_m else None
                                    cb_content = re.sub(r"(?:^|\s)([a-eA-E])(?:\.|\)|\:)\s*", "", clean_cb).strip()
                                    norm_cb_content = normalize_str(cb_content)

                                    should_check = False
                                    if cb_letter and cb_letter in mentioned_letters:
                                        should_check = True
                                    elif len(norm_cb_content) >= 5 and norm_cb_content in norm_target_cb:
                                        should_check = True
                                    elif len(norm_cb_content) >= 8:
                                        w_cb = set(w for w in norm_cb_content.split() if len(w) > 3)
                                        w_targ = set(w for w in norm_target_cb.split() if len(w) > 3)
                                        if w_cb and (len(w_cb & w_targ) / len(w_cb)) >= 0.6:
                                            should_check = True

                                    if should_check:
                                        await asyncio.sleep(random.uniform(0.5, 1.2))
                                        try:
                                            await cb_inp.check(timeout=4000)
                                        except Exception:
                                            await cb_inp.evaluate("el => { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); el.dispatchEvent(new Event('input', { bubbles: true })); }")
                                        total_filled += 1
                                        console.print(f"  ✔ [Q{q_num}] Checkbox marcado: [{cb_letter or '?'}] {cb_content[:35]}")
                                        await _emit_log(on_log, f"✔ [Q{q_num}] Checkbox marcado: [{cb_letter or '?'}] {cb_content[:25]}")

                        # 3. Menus Suspensos / Comboboxes (select) - Imune ao embaralhamento de linhas do Moodle
                        selects = q_el.locator("select")
                        s_count = await selects.count()
                        for s_idx in range(s_count):
                            sel_el = selects.nth(s_idx)
                            token_key = f"CAMPO_{global_input_idx}"

                            # Rótulo textual da linha/pergunta associada ao select
                            clean_row_label = await sel_el.evaluate('''el => {
                                // 1. Em tabelas de associação (qtype_match), a linha é um <tr>
                                const tr = el.closest('tr');
                                if (tr) {
                                    const clone = tr.cloneNode(true);
                                    clone.querySelectorAll('select, .accesshide, .sr-only').forEach(e => e.remove());
                                    const txt = clone.innerText.trim();
                                    if (txt) return txt;
                                }
                                // 2. Em layouts de div/grid, busca o container que contém o texto da linha
                                const row = el.closest('.form-inline, [class*="match_row"], div.row');
                                if (row) {
                                    const clone = row.cloneNode(true);
                                    clone.querySelectorAll('select, .accesshide, .sr-only').forEach(e => e.remove());
                                    const txt = clone.innerText.trim();
                                    if (txt) return txt;
                                }
                                // 3. Fallback: sobe na árvore até achar o bloco com texto descritivo
                                let p = el.parentElement;
                                while (p && p !== document.body && !p.classList.contains('que')) {
                                    const clone = p.cloneNode(true);
                                    clone.querySelectorAll('select, .accesshide, .sr-only').forEach(e => e.remove());
                                    const txt = clone.innerText.trim();
                                    if (txt && txt.length > 3) return txt;
                                    p = p.parentElement;
                                }
                                return '';
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
                            
                            # 1. Se houver rótulo na linha (ex: "1978 - The birth of Baby Louise..."), busca no answers_dict quem associa com este rótulo
                            if clean_row_label:
                                norm_lbl = normalize_str(clean_row_label)
                                words_lbl = set(w for w in norm_lbl.split() if len(w) > 2)

                                best_assoc_score = -1
                                best_assoc_val = None

                                for k, v in answers_dict.items():
                                    left, right = split_association_item(str(v))
                                    if left and right:
                                        norm_left = normalize_str(left)
                                        words_left = set(w for w in norm_left.split() if len(w) > 2)
                                        
                                        score = 0
                                        if norm_lbl == norm_left:
                                            score = 100
                                        elif (norm_lbl in norm_left or norm_left in norm_lbl) and min(len(norm_lbl), len(norm_left)) >= 8:
                                            score = 90
                                        elif words_lbl and words_left:
                                            overlap = len(words_lbl & words_left)
                                            jaccard = overlap / max(len(words_lbl), len(words_left))
                                            if jaccard >= 0.35:
                                                score = int(jaccard * 85)
                                        
                                        if score > best_assoc_score:
                                            best_assoc_score = score
                                            best_assoc_val = right

                                    elif norm_lbl == normalize_str(str(k)) or norm_lbl in normalize_str(str(k)):
                                        if 80 > best_assoc_score:
                                            best_assoc_score = 80
                                            best_assoc_val = str(v)

                                if best_assoc_score >= 40:
                                    target_val = best_assoc_val

                            # 2. Se não encontrou por rótulo, tenta por Q{q_num}_{s_idx+1} ou token ou Q{q_num} se s_count == 1
                            if not target_val:
                                if s_count == 1:
                                    target_val = (
                                        answers_dict.get(f"Q{q_num}") or
                                        answers_dict.get(f"Q{q_num}_1") or
                                        answers_dict.get(token_key)
                                    )
                                else:
                                    target_val = (
                                        answers_dict.get(f"Q{q_num}_{s_idx+1}") or 
                                        answers_dict.get(token_key)
                                    )

                            # Se o target_val contiver separador ("Nome → Resposta" ou "Item: Resposta"), extrai a resposta
                            if target_val:
                                _, clean_right = split_association_item(str(target_val))
                                if clean_right:
                                    target_val = clean_right

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
                                    try:
                                        await sel_el.select_option(value=best_opt["value"], timeout=4000)
                                        await sel_el.dispatch_event("change")
                                    except Exception:
                                        await sel_el.evaluate(
                                            "(el, val) => { el.value = val; el.dispatchEvent(new Event('change', { bubbles: true })); }",
                                            best_opt["value"]
                                        )
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

                        # 5. Arrastar e Soltar no Texto (Drag and Drop into text - ddwtos)
                        place_inputs = q_el.locator("input.placeinput, .drop.place input, input[name*='_p']")
                        p_count = await place_inputs.count()
                        if p_count > 0:
                            drag_homes = q_el.locator(".draghome, .drag")
                            dh_count = await drag_homes.count()
                            drag_options = []
                            for dh_idx in range(dh_count):
                                dh_el = drag_homes.nth(dh_idx)
                                dh_text = (await dh_el.inner_text()).strip()
                                dh_class = (await dh_el.get_attribute("class")) or ""
                                ch_match = re.search(r"choice(\d+)", dh_class)
                                ch_val = ch_match.group(1) if ch_match else str(dh_idx + 1)
                                drag_options.append({
                                    "choice": ch_val,
                                    "text": dh_text,
                                    "norm_text": normalize_str(dh_text),
                                    "locator": dh_el
                                })
                            
                            for p_idx in range(p_count):
                                p_inp = place_inputs.nth(p_idx)
                                token_key = f"CAMPO_{global_input_idx}"
                                target_val = (
                                    answers_dict.get(token_key) or
                                    answers_dict.get(f"Q{q_num}_{p_idx+1}") or
                                    answers_dict.get(f"Q{q_num}")
                                )
                                if target_val:
                                    norm_target = normalize_str(str(target_val))
                                    best_dh = None
                                    best_dh_score = -1
                                    for dh in drag_options:
                                        if norm_target == dh["norm_text"]:
                                            score = 100
                                        elif norm_target in dh["norm_text"] or dh["norm_text"] in norm_target:
                                            score = 85
                                        else:
                                            score = 0
                                        if score > best_dh_score:
                                            best_dh_score = score
                                            best_dh = dh
                                    
                                    if best_dh and best_dh_score >= 50:
                                        await asyncio.sleep(random.uniform(0.3, 0.7))
                                        await p_inp.evaluate(r"""(el, val) => {
                                            el.value = val;
                                            el.dispatchEvent(new Event('change', { bubbles: true }));
                                            el.dispatchEvent(new Event('input', { bubbles: true }));
                                        }""", best_dh["choice"])
                                        total_filled += 1
                                        console.print(f"  ✔ [{token_key}] Item arrastado: '{best_dh['text']}'")
                                        await _emit_log(on_log, f"✔ [{token_key}] Arrastado: '{best_dh['text'][:25]}'")

                                global_input_idx += 1

                    # Se for submissão definitiva (auto_submit=True), aciona 'Verificar' em cada questão antes de avançar
                    if auto_submit:
                        await self._verify_all_questions_on_current_attempt(page, on_log=on_log)

                    # Avançar página ou finalizar
                    finish_selectors = [
                        "input[name='next'][value*='Finalizar tentativa']",
                        "input[type='submit'][value*='Finalizar tentativa']",
                        "button:has-text('Finalizar tentativa')",
                        "a:has-text('Finalizar tentativa')",
                        "input[value*='Término da tentativa']",
                        "button:has-text('Término da tentativa')",
                        "a:has-text('Término da tentativa')",
                        "a.endtestlink",
                        "a[href*='summary.php']",
                        "form[action*='summary.php'] input[type='submit']",
                        "form[action*='summary.php'] button",
                        "button:has-text('Finish attempt')",
                        "input[value*='Finish attempt']",
                        "a:has-text('Finish attempt')"
                    ]
                    finish_btn = None
                    for f_sel in finish_selectors:
                        f_loc = page.locator(f_sel).first
                        if await f_loc.count() > 0 and await f_loc.is_visible():
                            finish_btn = f_loc
                            break

                    next_btn = page.locator(
                        "input[type='submit'][value*='Próxima página'], "
                        "button:has-text('Próxima página'), "
                        "button:has-text('Next page'), "
                        "input[value*='Next page']"
                    ).first

                    if await next_btn.count() > 0 and await next_btn.is_visible():
                        console.print("[dim]Avançando para a próxima página do questionário...[/dim]")
                        await next_btn.click()
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=12000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1000)
                    elif finish_btn:
                        console.print("[dim]Página final concluída. Indo para resumo da tentativa...[/dim]")
                        await finish_btn.click()
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)
                        has_next = False
                    else:
                        has_next = False

                # 3. Na tela de resumo (summary.php)
                if "summary.php" in page.url or await page.locator("button:has-text('Enviar tudo e terminar'), form[action*='processattempt.php']").count() > 0:
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
                    submit_btn_selectors = [
                        "#frm-finishattempt button",
                        "#frm-finishattempt input[type='submit']",
                        "form[action*='processattempt.php'] button",
                        "form[action*='processattempt.php'] input[type='submit']",
                        "button:has-text('Enviar tudo e terminar')",
                        "input[value*='Enviar tudo e terminar']",
                        "button:has-text('Submit all and finish')",
                        "input[value*='Submit all and finish']"
                    ]
                    submit_button = None
                    for s_sel in submit_btn_selectors:
                        loc = page.locator(s_sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            submit_button = loc
                            break

                    if submit_button:
                        await submit_button.scroll_into_view_if_needed()
                        await submit_button.click()

                        # Aguarda o modal de confirmação
                        try:
                            await page.wait_for_selector(".modal.show, [role='dialog'].show, .moodle-dialogue", timeout=6000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1000)

                        confirm_selectors = [
                            ".modal.show button[data-action='save']",
                            ".modal.show button.btn-primary:has-text('Enviar tudo e terminar')",
                            ".modal.show button[data-action='confirm']",
                            ".modal.show button.btn-primary:has-text('Submit all and finish')",
                            "div[role='dialog'] button[data-action='save']",
                            "div[role='dialog'] button.btn-primary:has-text('Enviar tudo e terminar')",
                            "div[role='dialog'] button.btn-primary",
                            ".modal.show button.btn-primary",
                            ".moodle-dialogue-confirm input[value*='Enviar tudo e terminar']",
                            ".confirmation-buttons button.btn-primary"
                        ]

                        confirm_clicked = False
                        for c_sel in confirm_selectors:
                            c_loc = page.locator(c_sel).first
                            if await c_loc.count() > 0 and await c_loc.is_visible():
                                await c_loc.click()
                                confirm_clicked = True
                                break

                        if not confirm_clicked:
                            await page.keyboard.press("Enter")

                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(2500)

                        # Verifica se o envio de fato foi concluído
                        is_submitted = "review.php" in page.url or "view.php" in page.url
                        if not is_submitted and ("summary.php" in page.url or "attempt.php" in page.url):
                            console.print("[red]Erro: Envio não foi concluído. Permaneceu em summary/attempt.[/red]")
                            return {
                                "success": False,
                                "error": "Falha ao enviar tudo e terminar: o Moodle permaneceu na tela de resumo."
                            }

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

                if auto_submit:
                    return {
                        "success": False,
                        "error": "Não foi possível avançar para a tela de resumo 'summary.php' para envio definitivo."
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
                try:
                    await page.goto(quiz_url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    await page.goto(quiz_url, timeout=25000)

                # 1. Se já estiver finalizado (review.php ou view.php com estado finalizado)
                if "review.php" in page.url:
                    screenshot_path = self.screenshots_dir / f"quiz_submitted_{int(time.time())}.png"
                    await page.screenshot(path=str(screenshot_path), full_page=False)
                    return {
                        "success": True,
                        "status": "already_submitted",
                        "review_url": page.url,
                        "screenshot_path": str(screenshot_path),
                        "message": "Questionário já se encontra finalizado no Moodle!"
                    }

                if "summary.php" not in page.url and "attempt.php" not in page.url:
                    opened = await self._open_or_resume_attempt(page, return_to_attempt=False)
                    if not opened and "summary.php" not in page.url and "attempt.php" not in page.url:
                        # Verifica se a tentativa já estava finalizada
                        if "review.php" in page.url or await page.locator(".cell.c1:has-text('Finalizada'), .generaltable td:has-text('Finalizada')").count() > 0:
                            screenshot_path = self.screenshots_dir / f"quiz_submitted_{int(time.time())}.png"
                            await page.screenshot(path=str(screenshot_path), full_page=False)
                            return {
                                "success": True,
                                "status": "already_submitted",
                                "review_url": page.url,
                                "screenshot_path": str(screenshot_path),
                                "message": "Questionário já se encontra finalizado no Moodle!"
                            }
                        return {
                            "success": False,
                            "error": "Não foi possível abrir ou retomar a tentativa para finalização no Moodle."
                        }

                # 2. Se estiver em attempt.php, percorre as páginas até chegar em summary.php
                if "attempt.php" in page.url:
                    has_next_page = True
                    max_hops = 30
                    hops = 0
                    while has_next_page and hops < max_hops and "attempt.php" in page.url:
                        hops += 1
                        # Executa 'Verificar' se a atividade possuir botões individuais por questão
                        await self._verify_all_questions_on_current_attempt(page, on_log=on_log)

                        next_page_btn = page.locator(
                            "input[type='submit'][value*='Próxima página'], "
                            "button:has-text('Próxima página'), "
                            "button:has-text('Next page'), "
                            "input[value*='Next page']"
                        ).first

                        if await next_page_btn.count() > 0 and await next_page_btn.is_visible():
                            console.print("[dim]Avançando para a próxima página do questionário...[/dim]")
                            await next_page_btn.click()
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=12000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(1000)
                        else:
                            # Última página da tentativa: clica em 'Finalizar tentativa...'
                            finish_selectors = [
                                "input[name='next'][value*='Finalizar tentativa']",
                                "input[type='submit'][value*='Finalizar tentativa']",
                                "button:has-text('Finalizar tentativa')",
                                "a:has-text('Finalizar tentativa')",
                                "input[value*='Término da tentativa']",
                                "button:has-text('Término da tentativa')",
                                "a:has-text('Término da tentativa')",
                                "a.endtestlink",
                                "a[href*='summary.php']",
                                "form[action*='summary.php'] input[type='submit']",
                                "form[action*='summary.php'] button",
                                "button:has-text('Finish attempt')",
                                "input[value*='Finish attempt']",
                                "a:has-text('Finish attempt')"
                            ]
                            finish_btn = None
                            for f_sel in finish_selectors:
                                f_loc = page.locator(f_sel).first
                                if await f_loc.count() > 0 and await f_loc.is_visible():
                                    finish_btn = f_loc
                                    break

                            if finish_btn:
                                console.print("[dim]Avançando para o resumo da tentativa no Moodle...[/dim]")
                                await finish_btn.click()
                                try:
                                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                except Exception:
                                    pass
                                await page.wait_for_timeout(1500)
                            has_next_page = False

                # 3. Na tela de resumo (summary.php)
                submit_btn_selectors = [
                    "#frm-finishattempt button",
                    "#frm-finishattempt input[type='submit']",
                    "form[action*='processattempt.php'] button",
                    "form[action*='processattempt.php'] input[type='submit']",
                    "button:has-text('Enviar tudo e terminar')",
                    "input[value*='Enviar tudo e terminar']",
                    "button:has-text('Submit all and finish')",
                    "input[value*='Submit all and finish']"
                ]

                submit_button = None
                for s_sel in submit_btn_selectors:
                    loc = page.locator(s_sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        submit_button = loc
                        break

                if not submit_button:
                    try:
                        await page.wait_for_selector(
                            "#frm-finishattempt button, button:has-text('Enviar tudo e terminar'), input[value*='Enviar tudo e terminar']",
                            timeout=8000
                        )
                        for s_sel in submit_btn_selectors:
                            loc = page.locator(s_sel).first
                            if await loc.count() > 0 and await loc.is_visible():
                                submit_button = loc
                                break
                    except Exception:
                        pass

                if submit_button:
                    await _emit_log(on_log, "Confirmando 'Enviar tudo e terminar' no Moodle...")
                    await submit_button.scroll_into_view_if_needed()
                    await submit_button.click()

                    # Aguarda o modal de confirmação
                    try:
                        await page.wait_for_selector(".modal.show, [role='dialog'].show, .moodle-dialogue", timeout=6000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1000)

                    confirm_selectors = [
                        ".modal.show button[data-action='save']",
                        ".modal.show button.btn-primary:has-text('Enviar tudo e terminar')",
                        ".modal.show button[data-action='confirm']",
                        ".modal.show button.btn-primary:has-text('Submit all and finish')",
                        "div[role='dialog'] button[data-action='save']",
                        "div[role='dialog'] button.btn-primary:has-text('Enviar tudo e terminar')",
                        "div[role='dialog'] button.btn-primary",
                        ".modal.show button.btn-primary",
                        ".moodle-dialogue-confirm input[value*='Enviar tudo e terminar']",
                        ".confirmation-buttons button.btn-primary"
                    ]

                    confirm_clicked = False
                    for c_sel in confirm_selectors:
                        c_loc = page.locator(c_sel).first
                        if await c_loc.count() > 0 and await c_loc.is_visible():
                            await c_loc.click()
                            confirm_clicked = True
                            break

                    if not confirm_clicked:
                        await page.keyboard.press("Enter")

                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(2500)

                    # Verifica com rigor se o envio de fato foi concluído
                    is_submitted = "review.php" in page.url or "view.php" in page.url
                    if not is_submitted and ("summary.php" in page.url or "attempt.php" in page.url):
                        console.print("[red]Erro: Envio não foi concluído no Moodle. Permaneceu na tela de resumo/tentativa.[/red]")
                        await _emit_log(on_log, "❌ Erro: Envio não foi concluído. Permaneceu na tela de resumo.")
                        return {
                            "success": False,
                            "error": "Falha ao enviar tudo e terminar: o Moodle permaneceu na tela de resumo da tentativa."
                        }

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
