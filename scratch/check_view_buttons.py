import asyncio
from playwright.async_api import async_playwright
from src.auth.moodle_auth import MoodleAuth

async def check_btn():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()
        await page.goto('https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45694', wait_until='networkidle')
        btns = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll("button, a.btn, input[type='submit']")).map(b => ({
                text: b.innerText ? b.innerText.trim() : b.value,
                href: b.href || '',
                formAction: b.form ? b.form.action : ''
            }));
        }''')
        print("Buttons found on view.php:")
        for b in btns:
            if b['text']:
                print(" ", b)
        await browser.close()

if __name__ == '__main__':
    asyncio.run(check_btn())
