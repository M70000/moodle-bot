"""Automação de Tarefas de Programação (Coding Tasks) no Canvas LMS via Playwright.

Identifica editores de código embutidos (Ace Editor, CodeMirror, Textarea e iFrames),
injeta soluções em Python/código, executa testes no console ('Run'), gera o 'Snapshot to URL'
e preenche/submete a URL na aba 'Web URL' do Canvas LMS.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from playwright.async_api import Frame, Page, async_playwright
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


def extract_code_from_text(text: str) -> str:
    """Extrai blocos de código limpos de textos gerados pela IA ou markdown."""
    if not text:
        return ""

    # Procura qualquer bloco fenced code ```lang ... ```
    m = re.search(r"```(?:\w+)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Se tiver linhas de código evidentes
    lines = text.strip().splitlines()
    code_lines = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
        elif line.strip().startswith(("print(", "def ", "import ", "for ", "if ", "#", "while ", "return ", "function ", "const ", "let ", "var ")):
            code_lines.append(line)

    if code_lines:
        return "\n".join(code_lines).strip()

    return text.strip()


extract_python_code_from_text = extract_code_from_text


class CanvasCodingAutomator:
    """Automação especializada para atividades interativas com editor de código no Canvas."""

    def __init__(self, auth: Optional[CanvasAuth] = None):
        self.auth = auth or CanvasAuth()

    async def _find_editor_frame(self, page: Page, max_wait_seconds: int = 15) -> Tuple[Optional[Any], str]:
        """Localiza o contêiner ou iframe onde o editor de código e botões estão hospedados."""
        # 1. Rola a página até qualquer elemento de iframe para ativar iframes com loading='lazy'
        try:
            lazy_iframes = page.locator("#online-editor, iframe[src*='editor'], iframe[src*='csin'], iframe[src*='code4'], div[id*='editor'] iframe, iframe")
            if await lazy_iframes.count() > 0:
                await lazy_iframes.first.scroll_into_view_if_needed()
        except Exception:
            pass

        # 2. Polling progressivo aguardando montagem do editor e scripts assíncronos (skulpt, ace, etc.)
        for attempt in range(max_wait_seconds):
            # Verifica no documento principal
            try:
                main_has_btn = await page.locator("#runButton, button:has-text('Run'), #codestoreURL, button:has-text('Snapshot')").count() > 0
                main_has_editor = await page.locator(".ace_editor, .CodeMirror, textarea[id*='code']").count() > 0
                if main_has_btn or main_has_editor:
                    return page, "main"
            except Exception:
                pass

            # Verifica em todos os iframes da página
            for idx, frame in enumerate(page.frames):
                try:
                    has_btn = await frame.locator("#runButton, #codestoreURL, button:has-text('Run'), button:has-text('Snapshot')").count() > 0
                    has_editor = await frame.locator(".ace_editor, .CodeMirror, textarea").count() > 0
                    if has_btn or has_editor:
                        return frame, f"iframe_{idx}"

                    # Se a URL do frame for conhecida de editores (ex: csinschools, trinket, code4)
                    if any(k in frame.url.lower() for k in ["csin", "editor.html", "code4.me", "trinket", "repl.it"]):
                        try:
                            await frame.wait_for_selector("#runButton, .ace_editor, textarea", timeout=2000)
                            return frame, f"iframe_{idx}"
                        except Exception:
                            pass
                except Exception:
                    continue

            await asyncio.sleep(1.0)

        return None, "none"

    async def inspect_coding_task(self, assignment_url: str, on_log: Optional[Any] = None) -> Dict[str, Any]:
        """Acessa a atividade e extrai instruções, requisitos e o código inicial com erro."""
        if not self.auth.session_exists:
            return {"has_coding_task": False, "error": "Sessão do Canvas não encontrada."}

        p = None
        browser = None
        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(self.auth.cookies_path))
            page = await context.new_page()

            await page.goto(assignment_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.0)

            # Extrai texto do enunciado e instruções
            desc_el = page.locator(".description, #assignment_show .description, .user_content")
            task_desc = ""
            if await desc_el.count() > 0:
                task_desc = await desc_el.first.inner_text()

            # Procura editor
            target_context, frame_type = await self._find_editor_frame(page, max_wait_seconds=12)
            if not target_context:
                return {
                    "has_coding_task": False,
                    "task_description": task_desc,
                    "error": "Editor de código não encontrado na página."
                }

            # Extrai o código inicial presente no editor
            initial_code = await target_context.evaluate(r'''() => {
                // 1. Ace Editor
                if (window.ace && document.querySelector('.ace_editor')) {
                    try {
                        const ed = window.ace.edit(document.querySelector('.ace_editor'));
                        return ed.getValue();
                    } catch (e) {}
                }
                const aceEl = document.querySelector('.ace_editor');
                if (aceEl && aceEl.env && aceEl.env.editor) {
                    try {
                        return aceEl.env.editor.getValue();
                    } catch (e) {}
                }

                // 2. CodeMirror
                const cmEl = document.querySelector('.CodeMirror');
                if (cmEl && cmEl.CodeMirror) {
                    try {
                        return cmEl.CodeMirror.getValue();
                    } catch (e) {}
                }

                // 3. Textarea
                const ta = document.querySelector("textarea[id*='code'], textarea");
                if (ta && ta.value) {
                    return ta.value;
                }

                // 4. Pre / Code dentro do editor
                const pre = document.querySelector(".editor pre, .code-editor pre");
                if (pre) {
                    return pre.innerText;
                }

                return "";
            }''')

            return {
                "has_coding_task": True,
                "frame_type": frame_type,
                "initial_code": initial_code.strip() if initial_code else "",
                "task_description": task_desc.strip(),
                "supports_snapshot": True
            }

        except Exception as e:
            console.print(f"[yellow]Aviso ao inspecionar tarefa de código: {e}[/yellow]")
            return {"has_coding_task": False, "error": str(e)}
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

    async def _safe_click_button(self, locator, context_or_frame, fallback_js: Optional[str] = None) -> bool:
        """Executa clique de forma robusta e à prova de timeouts em botões de editores interativos."""
        try:
            await locator.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass

        try:
            await locator.click(force=True, timeout=2500)
            return True
        except Exception:
            pass

        try:
            await locator.dispatch_event("click")
            return True
        except Exception:
            pass

        if fallback_js and context_or_frame:
            try:
                await context_or_frame.evaluate(fallback_js)
                return True
            except Exception:
                pass
        return False

    async def solve_and_snapshot(
        self,
        assignment_url: str,
        solution_code: str,
        auto_submit: bool = False,
        on_log: Optional[Any] = None
    ) -> Tuple[bool, str, Optional[str]]:
        """Injeta a solução no editor, clica em Run, captura o Snapshot to URL e preenche/submete na aba Web URL."""
        if not self.auth.session_exists:
            return False, "Sessão do Canvas não encontrada.", None

        clean_code = extract_python_code_from_text(solution_code)
        if not clean_code:
            return False, "Código de resolução vazio ou inválido.", None

        p = None
        browser = None
        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(self.auth.cookies_path))
            page = await context.new_page()

            snapshot_captured_url = None

            # Intercepta diálogos nativos (window.prompt / alert)
            async def _handle_dialog(dialog):
                nonlocal snapshot_captured_url
                msg = dialog.message or ""
                val = dialog.default_value or ""
                for candidate in (msg, val):
                    found = re.findall(r"https?://[^\s\)\"\'<>]+", candidate)
                    if found:
                        snapshot_captured_url = found[0]
                await dialog.accept()

            page.on("dialog", lambda d: asyncio.create_task(_handle_dialog(d)))

            # Intercepta requisições de rede para APIs de snapshot
            async def _handle_response(resp):
                nonlocal snapshot_captured_url
                try:
                    url_low = resp.url.lower()
                    if "codestore" in url_low and "put" in url_low:
                        text = (await resp.text()).strip()
                        if text and len(text) < 40 and not text.startswith("<") and not text.startswith("{"):
                            snapshot_captured_url = f"https://csinschools.io/editor/editor.html?&nosave=1&nofs=1&id={text}"
                    elif "snapshot" in url_low:
                        text = await resp.text()
                        found = re.findall(r"https?://[^\s\)\"\'<>{}\"]+", text)
                        for u in found:
                            if "snapshot" in u.lower() or "trinket" in u.lower() or "canvas" in u.lower() or "csin" in u.lower():
                                snapshot_captured_url = u
                                break
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.create_task(_handle_response(r)))

            await _emit_log(on_log, "Carregando atividade interativa no Canvas...")
            await page.goto(assignment_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.0)

            # Localiza contexto do editor (página ou iframe) com busca progressiva
            target_context, frame_type = await self._find_editor_frame(page, max_wait_seconds=15)
            if not target_context:
                return False, "Editor de código interativo não encontrado na página.", None

            await _emit_log(on_log, f"Injetando código corrigido no editor ({frame_type})...")

            # Injeta código no editor
            injected = await target_context.evaluate(r'''(code) => {
                // 1. Ace Editor
                if (window.ace && document.querySelector('.ace_editor')) {
                    try {
                        const ed = window.ace.edit(document.querySelector('.ace_editor'));
                        ed.setValue(code, -1);
                        return true;
                    } catch (e) {}
                }
                const aceEl = document.querySelector('.ace_editor');
                if (aceEl && aceEl.env && aceEl.env.editor) {
                    try {
                        aceEl.env.editor.setValue(code, -1);
                        return true;
                    } catch (e) {}
                }

                // 2. CodeMirror
                const cmEl = document.querySelector('.CodeMirror');
                if (cmEl && cmEl.CodeMirror) {
                    try {
                        cmEl.CodeMirror.setValue(code);
                        return true;
                    } catch (e) {}
                }

                // 3. Textarea
                const ta = document.querySelector("textarea[id*='code'], textarea");
                if (ta) {
                    ta.value = code;
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }

                return false;
            }''', clean_code)

            if not injected:
                # Fallback: clica no editor e digita
                editor_box = target_context.locator(".ace_editor, .CodeMirror, textarea").first
                if await editor_box.count() > 0:
                    try:
                        await editor_box.click(force=True, timeout=2500)
                    except Exception:
                        pass
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.insert_text(clean_code)

            await asyncio.sleep(1.0)

            # Clica no botão Run de forma robusta e imediata
            run_btn = target_context.locator("#runButton, button:has-text('Run'), a:has-text('Run'), [title*='Run']").first
            if await run_btn.count() > 0:
                await _emit_log(on_log, "⚡ Executando código corrigido no console ('Run')...")
                run_fallback = """() => {
                    const b = document.querySelector("#runButton, button[onclick*='run']");
                    if (b) b.click();
                    if (typeof runSkulpt === "function") runSkulpt(false);
                }"""
                await self._safe_click_button(run_btn, target_context, run_fallback)
                await asyncio.sleep(2.5)

            # Clica no botão Snapshot to URL de forma robusta e imediata
            snapshot_btn = target_context.locator("#codestoreURL, button:has-text('Snapshot to URL'), button:has-text('Snapshot'), a:has-text('Snapshot to URL'), [title*='Snapshot']").first
            if await snapshot_btn.count() > 0:
                await _emit_log(on_log, "🔗 Gerando link público ('Snapshot to URL')...")
                snap_fallback = """() => {
                    const b = document.querySelector("#codestoreURL, button[onclick*='getCodestoreURL'], button:has-text('Snapshot')");
                    if (b) b.click();
                    if (typeof getCodestoreURL === "function") getCodestoreURL();
                }"""
                await self._safe_click_button(snapshot_btn, target_context, snap_fallback)
                await asyncio.sleep(2.0)

            # Tenta capturar a URL gerada na tela caso não tenha vindo por diálogo
            if not snapshot_captured_url:
                for _ in range(8):
                    snapshot_captured_url = await target_context.evaluate(r'''() => {
                        // 1. Tag dialog e urlContent (CS in Schools)
                        const diaLink = document.querySelector("#urlDialog a, #urlContent a, dialog a");
                        if (diaLink && diaLink.href && diaLink.href.startsWith("http")) return diaLink.href;

                        const dia = document.querySelector("#urlDialog, dialog, #urlContent");
                        if (dia && dia.innerText && dia.innerText.includes("http")) {
                            const m = dia.innerText.match(/https?:\/\/[^\s\)\"\'<>]+/);
                            if (m) return m[0];
                        }

                        // 2. Inputs visíveis ou links recém-criados
                        const inputs = Array.from(document.querySelectorAll("input[type='text'], input[type='url'], input:not([type])"));
                        for (const inp of inputs) {
                            if (inp.value && inp.value.startsWith("http") && (inp.value.includes("snapshot") || inp.value.includes("trinket") || inp.value.includes("view") || inp.value.includes("run") || inp.value.includes("id="))) {
                                return inp.value;
                            }
                        }
                        const links = Array.from(document.querySelectorAll("a[href^='http']"));
                        for (const a of links) {
                            if (a.href && (a.href.includes("snapshot") || a.href.includes("trinket") || a.href.includes("id="))) {
                                return a.href;
                            }
                        }
                        return null;
                    }''')
                    if snapshot_captured_url:
                        break
                    await asyncio.sleep(1.0)

            # Se ainda não encontrou, busca no contexto principal da página
            if not snapshot_captured_url:
                snapshot_captured_url = await page.evaluate(r'''() => {
                    const inputs = Array.from(document.querySelectorAll("input[type='text'], input[type='url'], input:not([type])"));
                    for (const inp of inputs) {
                        if (inp.value && inp.value.startsWith("http") && (inp.value.includes("snapshot") || inp.value.includes("trinket") || inp.value.includes("id="))) {
                            return inp.value;
                        }
                    }
                    return null;
                }''')

            if not snapshot_captured_url:
                await _emit_log(on_log, "⚠️ Botão Snapshot clicado, mas a URL não pôde ser interceptada automaticamente. Prosseguindo com o envio do código.")
            else:
                await _emit_log(on_log, f"✔ Snapshot URL gerada com sucesso: {snapshot_captured_url}")

            # -------------------------------------------------------------
            # Preenchimento na Seção de Submissão do Canvas (Choose a submission type)
            # -------------------------------------------------------------
            # Localiza a aba "Web URL"
            web_url_tab = page.locator("button:has-text('Web URL'), a:has-text('Web URL'), li:has-text('Web URL'), [data-view*='web_url']").first
            if await web_url_tab.count() > 0:
                await _emit_log(on_log, "Abrindo aba 'Web URL' na seção de entrega do Canvas...")
                await web_url_tab.click()
                await asyncio.sleep(1.0)

            # Campo de entrada de URL
            url_input = page.locator("#submission_url, input[name='submission[url]'], input[type='url'], input[placeholder*='http']").first
            if await url_input.count() > 0 and snapshot_captured_url:
                await url_input.scroll_into_view_if_needed()
                await url_input.fill(snapshot_captured_url)
                await asyncio.sleep(0.5)

            if not auto_submit:
                # Modo Preencher
                msg = f"Código testado com sucesso! Snapshot URL gerada e inserida na aba Web URL: {snapshot_captured_url or 'N/A'}"
                await _emit_log(on_log, f"✔ {msg}")
                return True, msg, snapshot_captured_url

            # Modo Finalizar: Tentativa 1 - Via interface web do Canvas
            await _emit_log(on_log, "🚀 Enviando submissão definitiva na aba Web URL...")
            submitted_via_dom = False

            try:
                # 1. Se a tela de submissão não estiver aberta, clica no botão para abrir (Submit/Start Assignment)
                open_btn = page.locator(
                    "a.submit_assignment_link, a:has-text('Submit Assignment'), a:has-text('Start Assignment'), "
                    "a:has-text('Re-submit Assignment'), a:has-text('New Attempt'), "
                    "button:has-text('Submit Assignment'), button:has-text('Start Assignment'), button:has-text('New Attempt')"
                ).first
                if await open_btn.count() > 0 and await open_btn.is_visible():
                    await open_btn.click()
                    await asyncio.sleep(1.5)

                # 2. Re-localiza e clica na aba Web URL
                web_url_tab = page.locator("button:has-text('Web URL'), a:has-text('Web URL'), li:has-text('Web URL'), [data-view*='web_url'], [id*='tab-web_url']").first
                if await web_url_tab.count() > 0:
                    await web_url_tab.click()
                    await asyncio.sleep(1.0)

                # 3. Insere a Snapshot URL no input de submissão
                url_input = page.locator("#submission_url, input[name='submission[url]'], input[type='url'], input[placeholder*='http']").first
                if await url_input.count() > 0 and snapshot_captured_url:
                    await url_input.scroll_into_view_if_needed()
                    await url_input.fill(snapshot_captured_url)
                    await asyncio.sleep(0.5)

                # 4. Clica no botão final de confirmação de envio
                submit_btn = page.locator(
                    "#submit_assignment_form input[type='submit'], #submit_assignment_form button[type='submit'], "
                    "input#submit_button, button#submit_button, button[type='submit']:has-text('Submit'), "
                    "button:has-text('Submit Assignment'), input[value*='Submit Assignment']"
                ).last
                if await submit_btn.count() > 0:
                    await submit_btn.click()
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    await asyncio.sleep(2.0)
                    submitted_via_dom = True
            except Exception as dom_err:
                console.print(f"[yellow]Aviso no envio via DOM do Canvas: {dom_err}[/yellow]")

            if submitted_via_dom:
                msg = f"Atividade submetida com sucesso no Canvas via Web URL! Snapshot: {snapshot_captured_url}"
                await _emit_log(on_log, f"✔ {msg}")
                return True, msg, snapshot_captured_url

            # Modo Finalizar: Tentativa 2 (Infalível) - Via API REST v1 do Canvas
            if snapshot_captured_url:
                await _emit_log(on_log, "📡 Confirmando entrega oficial via API REST do Canvas...")
                try:
                    from src.providers.canvas import CanvasSubmitter, extract_canvas_ids
                    c_id, a_id = extract_canvas_ids(assignment_url)
                    if a_id:
                        rest_ok, rest_msg = await CanvasSubmitter().submit_url(
                            course_id=c_id or "101",
                            assignment_id=a_id,
                            url=snapshot_captured_url,
                            on_log=on_log
                        )
                        if rest_ok:
                            msg = f"Atividade submetida com sucesso no Canvas via API REST! Snapshot: {snapshot_captured_url}"
                            await _emit_log(on_log, f"✔ {msg}")
                            return True, msg, snapshot_captured_url
                        else:
                            await _emit_log(on_log, f"⚠️ API REST retornou: {rest_msg}")
                except Exception as rest_err:
                    await _emit_log(on_log, f"⚠️ Erro no fallback da API REST: {rest_err}")

            return False, f"Não foi possível concluir o envio da URL para a atividade ({snapshot_captured_url}).", snapshot_captured_url

        except Exception as e:
            console.print(f"[red]Erro na automação da tarefa de código: {e}[/red]")
            await _emit_log(on_log, f"❌ Erro na automação da tarefa de código: {e}")
            return False, f"Erro na automação do editor: {e}", None
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
