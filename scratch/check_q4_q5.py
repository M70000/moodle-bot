import asyncio
from playwright.async_api import async_playwright
from src.auth.moodle_auth import MoodleAuth

async def check_q4_q5():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()
        await page.goto('https://virtual.ufmg.br/20262/mod/quiz/attempt.php?attempt=73604&cmid=45694', wait_until='networkidle')
        
        q_els = await page.locator('.que').all()
        for idx, q in enumerate(q_els):
            q_id = await q.get_attribute('id')
            no_el = q.locator('.info .no')
            no_txt = await no_el.inner_text() if await no_el.count() > 0 else f'Item {idx+1}'
            inputs = await q.locator('input, textarea, select').all()
            print(f"[{q_id}] {no_txt} -> {len(inputs)} input elements")
            for inp in inputs:
                tag = await inp.evaluate('el => el.tagName')
                typ = await inp.get_attribute('type')
                name = await inp.get_attribute('name')
                val = await inp.get_attribute('value')
                if typ != 'hidden':
                    print(f"   <{tag} type='{typ}' name='{name}' value='{val}'>")

        await browser.close()

if __name__ == '__main__':
    asyncio.run(check_q4_q5())
