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
from typing import Any, Dict, List, Optional, Tuple

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


class MoodleQuizAutomator:
    """Controlador inteligente e humanizado para questionários do Moodle."""

    def __init__(self, auth: Optional[MoodleAuth] = None):
        self.auth = auth or MoodleAuth()
        self.screenshots_dir = Path("storage/submissions/screenshots")
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    async def inspect_and_extract_quiz(self, quiz_url: str) -> Dict[str, Any]:
        """Acessa a tentativa do questionário e extrai os enunciados reais com marcadores pontuais."""
        console.print(f"[cyan]Inspecionando estrutura real do quiz no Moodle: {quiz_url}[/cyan]")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=self.auth.cookies_path)
            page = await context.new_page()

            try:
                await page.goto(quiz_url, wait_until="networkidle")

                # Se já estiver na tentativa ou se houver botão para iniciar/continuar
                if "attempt.php" not in page.url:
                    attempt_btn = page.locator(
                        "form[action*='startattempt.php'] button, "
                        "form[action*='startattempt.php'] input[type='submit'], "
                        "button:has-text('tentativa'), "
                        "a:has-text('tentativa')"
                    ).first

                    if await attempt_btn.count() > 0:
                        console.print("[dim]Acessando tentativa para extrair perguntas reais...[/dim]")
                        await attempt_btn.click()
                        await page.wait_for_load_state("networkidle")
                    else:
                        return {
                            "success": False,
                            "error": "Não foi possível abrir a tentativa do questionário no Moodle."
                        }

                # Extração estruturada do DOM com mapeamento pontual de campos
                questions_data = await page.evaluate('''() => {
                    const questions = [];
                    const qNodes = document.querySelectorAll(".que");
                    let globalInputIdx = 1;

                    qNodes.forEach((q, qIndex) => {
                        const noText = q.querySelector(".info .no") ? q.querySelector(".info .no").innerText.trim() : `Questão ${qIndex + 1}`;
                        const contentEl = q.querySelector(".content") || q;
                        
                        const clone = contentEl.cloneNode(true);
                        const inputs = clone.querySelectorAll("input[type='text'], textarea");
                        const inputMap = [];

                        inputs.forEach(inp => {
                            const token = `[[CAMPO_${globalInputIdx}]]`;
                            inputMap.push({
                                token: token,
                                key: `CAMPO_${globalInputIdx}`,
                                name: inp.getAttribute("name"),
                                id: inp.getAttribute("id"),
                                index: globalInputIdx
                            });
                            const textNode = document.createTextNode(` ${token} `);
                            inp.parentNode.replaceChild(textNode, inp);
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

                        questions.push({
                            qIndex: qIndex + 1,
                            qNumberText: noText,
                            fullTextWithTokens: clone.innerText.trim(),
                            inputsCount: inputMap.length,
                            inputs: inputMap,
                            radios: radioOptions
                        });
                    });

                    return questions;
                }''')

                total_inputs = sum(q.get("inputsCount", 0) for q in questions_data)
                console.print(f"[green]✔ Perguntas extraídas com sucesso! Total de campos/lacunas: {total_inputs}[/green]")

                return {
                    "success": True,
                    "attempt_url": page.url,
                    "total_inputs": total_inputs,
                    "questions": questions_data
                }

            except Exception as e:
                console.print(f"[red]Erro ao extrair questões do quiz: {e}[/red]")
                return {
                    "success": False,
                    "error": str(e)
                }
            finally:
                await browser.close()

    async def fill_and_submit_quiz(
        self,
        quiz_url: str,
        answers: Any,
        auto_submit: bool = True,
        min_duration_seconds: int = 180
    ) -> Dict[str, Any]:
        """Preenche o questionário com digitação humana realista e garante tempo de tentativa seguro."""
        console.print(f"[cyan]Iniciando preenchimento humanizado do questionário: {quiz_url}[/cyan]")
        start_time = time.time()

        # Converte respostas para um mapa chave-valor direto
        answers_dict: Dict[str, str] = {}
        if isinstance(answers, dict):
            answers_dict = {str(k).upper().replace("[[", "").replace("]]", ""): str(v) for k, v in answers.items()}
        elif isinstance(answers, list):
            for item in answers:
                if isinstance(item, dict):
                    if "key" in item and "value" in item:
                        answers_dict[str(item["key"]).upper()] = str(item["value"])
                    elif "question" in item:
                        ans_val = item.get("answer") or item.get("answers")
                        if isinstance(ans_val, list):
                            for s_idx, sv in enumerate(ans_val):
                                answers_dict[f"Q{item['question']}_{s_idx+1}"] = str(sv)
                        else:
                            answers_dict[f"Q{item['question']}"] = str(ans_val)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=self.auth.cookies_path)
            page = await context.new_page()

            try:
                await page.goto(quiz_url, wait_until="networkidle")

                # Se não estiver em attempt.php, clica no botão de tentativa
                if "attempt.php" not in page.url:
                    attempt_btn = page.locator(
                        "form[action*='startattempt.php'] button, "
                        "form[action*='startattempt.php'] input[type='submit'], "
                        "button:has-text('tentativa'), "
                        "a:has-text('tentativa')"
                    ).first

                    if await attempt_btn.count() > 0:
                        await attempt_btn.click()
                        await page.wait_for_load_state("networkidle")

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
                                
                                # Pausa natural entre preenchimentos (simulando leitura)
                                await asyncio.sleep(random.uniform(1.2, 3.0))

                            global_input_idx += 1

                        # 2. Alternativas de Múltipla Escolha (Radio buttons)
                        radios = q_el.locator("input[type='radio']")
                        r_count = await radios.count()
                        if r_count > 0:
                            target_val = answers_dict.get(f"Q{q_num}") or answers_dict.get(f"CAMPO_{global_input_idx}")
                            if target_val:
                                norm_target = normalize_str(target_val)
                                options = q_el.locator(".answer > div, .answer table tr, .answer label")
                                opt_count = await options.count()
                                for o_idx in range(opt_count):
                                    opt_text = await options.nth(o_idx).inner_text()
                                    if norm_target in normalize_str(opt_text) or normalize_str(opt_text) in norm_target:
                                        r_btn = options.nth(o_idx).locator("input[type='radio']")
                                        if await r_btn.count() > 0:
                                            await asyncio.sleep(random.uniform(1.0, 2.5))
                                            await r_btn.check()
                                            total_filled += 1
                                            console.print(f"  ✔ [Q{q_num}] Alternativa marcada: {opt_text[:40]}")
                                            break

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

                # 3. Na tela de resumo (summary.php): Simulação do Tempo Seguro
                if "summary.php" in page.url or await page.locator("button:has-text('Enviar tudo e terminar')").count() > 0:
                    elapsed = time.time() - start_time
                    target_duration = max(min_duration_seconds, total_filled * 8)
                    
                    if elapsed < target_duration:
                        wait_seconds = int(target_duration - elapsed)
                        console.print(
                            f"[bold yellow]⏳ Simulando tempo de leitura e revisão humana do aluno "
                            f"(aguardando {wait_seconds}s para atingir tempo seguro de {int(target_duration)}s no Moodle)...[/bold yellow]"
                        )
                        # Heartbeat em intervalos para manter o usuário informado
                        while wait_seconds > 0:
                            step = min(wait_seconds, 15)
                            await asyncio.sleep(step)
                            wait_seconds -= step
                            if wait_seconds > 0:
                                console.print(f"[dim]⏳ Revisando tentativa... faltam {wait_seconds}s para submissão definitiva...[/dim]")

                    if not auto_submit:
                        return {
                            "success": True,
                            "status": "draft_saved",
                            "total_filled": total_filled,
                            "summary_url": page.url,
                            "message": "Respostas preenchidas no Moodle em modo rascunho com tempo seguro."
                        }

                    console.print("[bold green]Confirmando envio definitivo no Moodle...[/bold green]")
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
                return {
                    "success": False,
                    "error": str(e)
                }
            finally:
                await browser.close()
