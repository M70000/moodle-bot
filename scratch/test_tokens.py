import asyncio
from playwright.async_api import async_playwright
from src.auth.moodle_auth import MoodleAuth

async def print_tokens():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()
        await page.goto('https://virtual.ufmg.br/20262/mod/quiz/attempt.php?attempt=73604&cmid=45694', wait_until='networkidle')
        
        text = await page.evaluate('''() => {
            const q = document.querySelectorAll(".que")[1];
            const clone = q.querySelector(".content").cloneNode(true);
            let idx = 1;
            clone.querySelectorAll("input[type='text']").forEach(inp => {
                const tn = document.createTextNode(" [[CAMPO_" + idx + "]] ");
                inp.parentNode.replaceChild(tn, inp);
                idx++;
            });
            return clone.innerText;
        }''')
        print(text)
        await browser.close()

if __name__ == '__main__':
    asyncio.run(print_tokens())
