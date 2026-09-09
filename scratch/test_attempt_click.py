import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth.moodle_auth import MoodleAuth

async def check():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()
        await page.goto("https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45694", wait_until="networkidle")
        print("Initial URL:", page.url)
        
        # Check all possible attempt buttons:
        selectors = [
            "form[action*='startattempt.php'] button",
            "form[action*='startattempt.php'] input[type='submit']",
            "button:has-text('tentativa')",
            "a:has-text('tentativa')",
            "button:has-text('Fazer uma outra tentativa')",
            "button:has-text('Continuar sua tentativa')",
            "button:has-text('Tentar novamente')"
        ]
        
        for sel in selectors:
            loc = page.locator(sel)
            cnt = await loc.count()
            if cnt > 0:
                for i in range(cnt):
                    txt = await loc.nth(i).inner_text()
                    print(f"Found match for '{sel}' [{i}]: '{txt.strip()}'")
                    
        # Let's test clicking the startattempt button
        btn = page.locator("form[action*='startattempt.php'] button, form[action*='startattempt.php'] input[type='submit']").first
        if await btn.count() > 0:
            btn_txt = await btn.inner_text()
            print(f"Clicking: '{btn_txt}'...")
            await btn.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            print("After click URL:", page.url)
            
            # Check if there's a modal dialog asking to confirm start
            modals = page.locator(".modal.show, div[role='dialog']")
            if await modals.count() > 0:
                print("Modal appeared! Text:", await modals.first.inner_text())
                confirm = page.locator(".modal.show button, div[role='dialog'] button")
                print("Modal buttons:", await confirm.all_inner_texts())
                
        await browser.close()

if __name__ == "__main__":
    asyncio.run(check())
