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
        print("URL:", page.url)
        buttons = await page.locator("button, input[type='submit'], a.btn").all_inner_texts()
        print("Buttons on page:", [b.strip() for b in buttons if b.strip()])
        
        form_html = await page.evaluate("""() => {
            const f = document.querySelector("form[action*='startattempt.php']");
            return f ? f.outerHTML : "NO STARTATTEMPT FORM";
        }""")
        print("StartAttempt Form HTML:\n", form_html)
        
        # Check table / feedback / summary
        page_text = await page.inner_text("body")
        for line in page_text.splitlines():
            if any(k in line.lower() for k in ["tentativa", "nota", "estado", "finalizada", "reavaliar"]):
                print("  [MATCH]", line.strip())
                
        await browser.close()

if __name__ == "__main__":
    asyncio.run(check())
