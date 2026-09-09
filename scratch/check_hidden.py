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
        await page.goto("https://virtual.ufmg.br/20262/mod/quiz/attempt.php?attempt=73604&cmid=45694", wait_until="networkidle")
        
        info = await page.evaluate("""() => {
            const res = [];
            document.querySelectorAll('.que').forEach((q, idx) => {
                const contentEl = q.querySelector('.content') || q;
                const clone = contentEl.cloneNode(true);
                
                // Remove todos os elementos de acessibilidade, botões e controles internos do Moodle
                clone.querySelectorAll('.accesshide, .sr-only, .im-controls, input[type=\"submit\"], button, .comment, .feedback').forEach(el => el.remove());
                
                // Substitui inputs por [[CAMPO_X]]
                clone.querySelectorAll("input[type='text'], textarea").forEach((inp, iIdx) => {
                    const token = document.createTextNode(` [[CAMPO_${iIdx+1}]] `);
                    inp.parentNode.replaceChild(token, inp);
                });
                
                res.push({ q: idx + 1, text: clone.innerText.trim() });
            });
            return res;
        }""")
        for item in info:
            print(f"--- Q{item['q']} ---")
            print(item['text'][:300])
        await browser.close()

if __name__ == "__main__":
    asyncio.run(check())
