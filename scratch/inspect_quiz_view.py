import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from src.auth.moodle_auth import MoodleAuth

async def inspect_quiz():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()
        
        # Open Unidade 2 :: Aula 2 (id=45700)
        url = "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45700"
        print("Navigating to:", url)
        await page.goto(url, wait_until="networkidle")
        
        title = await page.title()
        print("Page Title:", title)
        
        # Check buttons or forms available on this page
        buttons = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll("button, input[type='submit'], a.btn")).map(b => ({
                tag: b.tagName,
                text: b.innerText.trim(),
                value: b.value,
                href: b.href || '',
                formAction: b.form ? b.form.action : ''
            }));
        }''')
        print("Buttons found:")
        for b in buttons:
            print(" ", b)
            
        # Check if there is a form to start attempt
        forms = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll("form")).map(f => ({
                action: f.action,
                method: f.method,
                inputs: Array.from(f.querySelectorAll("input")).map(i => ({ name: i.name, type: i.type, value: i.value }))
            }));
        }''')
        print("Forms found:")
        for f in forms:
            print(" ", f)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(inspect_quiz())
