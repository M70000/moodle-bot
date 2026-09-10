import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.auth.moodle_auth import MoodleAuth

async def test_submit():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()

        url = "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45769"
        await page.goto(url, wait_until="networkidle")
        print("1. View URL:", page.url)

        # Clica em continuar tentativa
        cont_btn = page.locator("button:has-text('Continuar sua tentativa'), a:has-text('Continuar sua tentativa')").first
        if await cont_btn.count() > 0:
            await cont_btn.click()
            await page.wait_for_load_state("networkidle")
            print("2. Resumed URL:", page.url)

        # Se estiver em attempt.php, clica em Finalizar tentativa ...
        if "attempt.php" in page.url:
            finish_btn = page.locator("input[name='next'][value*='Finalizar tentativa'], input[type='submit'][value*='Finalizar tentativa'], button:has-text('Finalizar tentativa')").first
            if await finish_btn.count() > 0:
                print("3. Clicando em Finalizar tentativa ...")
                await finish_btn.click()
                await page.wait_for_load_state("networkidle")
                print("4. Summary URL:", page.url)

        # Agora está em summary.php! Clica em Enviar tudo e terminar
        if "summary.php" in page.url:
            submit_btn = page.locator("#frm-finishattempt button, button:has-text('Enviar tudo e terminar')").first
            print("5. Clicando no botão do formulário de Enviar tudo e terminar...")
            await submit_btn.click()
            await page.wait_for_timeout(1000)

            # Espera o modal aparecer e clica no botão de confirmação com data-action='save'
            modal_confirm = page.locator(".modal.show button[data-action='save'], .modal.show button.btn-primary:has-text('Enviar tudo e terminar')").first
            print(f"6. Modal confirm button count: {await modal_confirm.count()}")
            if await modal_confirm.count() > 0:
                print("7. Clicando na confirmação do modal (data-action='save')...")
                await modal_confirm.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(2000)
                print("8. Final URL após submissão definitiva:", page.url)
                print("   Is review.php?", "review.php" in page.url)

        await page.screenshot(path="scratch/quiz_45769_after_real_submit.png")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_submit())
