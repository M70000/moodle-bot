import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.auth.moodle_auth import MoodleAuth

async def inspect():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()

        url = "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45769"
        await page.goto(url, wait_until="networkidle")
        print("Initial URL:", page.url)

        buttons = await page.locator("button, input[type='submit'], a.btn").all_inner_texts()
        print("Buttons on view.php:", [b.strip() for b in buttons if b.strip()])

        # Se tiver botão de continuar ou fazer tentativa
        continue_btn = page.locator("button:has-text('Continuar'), a:has-text('Continuar'), button:has-text('tentativa'), a:has-text('tentativa'), form[action*='startattempt.php'] button").first
        if await continue_btn.count() > 0:
            txt = (await continue_btn.inner_text()).strip()
            print(f"Clicking: '{txt}'")
            await continue_btn.click()
            await page.wait_for_load_state("networkidle")
            print("After click URL:", page.url)

        # Clica em Finalizar tentativa ...
        finish_btn = page.locator("input[name='next'][value*='Finalizar tentativa'], input[type='submit'][value*='Finalizar tentativa'], button:has-text('Finalizar tentativa')").first
        if await finish_btn.count() > 0:
            print("Clicando em Finalizar tentativa...")
            await finish_btn.click()
            await page.wait_for_load_state("networkidle")
            print("URL após finalizar tentativa:", page.url)

        # Clica em Enviar tudo e terminar
        submit_btn = page.locator("#frm-finishattempt button, button:has-text('Enviar tudo e terminar')").first
        print(f"Submit button found: {await submit_btn.count()}")
        await submit_btn.click()
        await page.wait_for_timeout(1500)

        # Inspeciona modais que surgiram
        modals = await page.evaluate(r'''() => {
            return Array.from(document.querySelectorAll(".modal, [role='dialog'], .moodle-dialogue")).map(m => ({
                id: m.id,
                className: m.className,
                style: m.getAttribute('style'),
                visible: m.offsetWidth > 0 && m.offsetHeight > 0,
                title: (m.querySelector('.modal-title, .moodle-dialogue-hd') || {}).innerText,
                buttons: Array.from(m.querySelectorAll("button, input[type='submit'], input[type='button'], a.btn")).map(b => ({
                    tag: b.tagName,
                    text: b.innerText.trim(),
                    value: b.value,
                    action: b.getAttribute('data-action'),
                    className: b.className,
                    visible: b.offsetWidth > 0 && b.offsetHeight > 0
                }))
            }));
        }''')
        print(f"\n--- Modais pós clique ({len(modals)}) ---")
        for m in modals:
            print(m)

        await page.screenshot(path="scratch/quiz_45769_modal.png")



        # Verifica se tem formulários de envio (processattempt.php, summary.php, etc.)
        forms = await page.evaluate(r'''() => {
            return Array.from(document.querySelectorAll("form")).map(f => ({
                action: f.action,
                method: f.method,
                inputs: Array.from(f.querySelectorAll("input, button")).map(i => ({
                    name: i.name,
                    value: i.value || i.innerText.trim(),
                    type: i.type
                }))
            }));
        }''')
        print(f"\n--- Formulários ({len(forms)}) ---")
        for f in forms:
            print(f"Action: {f['action']}")
            for inp in f['inputs']:
                print(f"   - {inp}")

        await page.screenshot(path="scratch/quiz_45769_current.png")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(inspect())
