import asyncio
from playwright.async_api import async_playwright
from src.auth.moodle_auth import MoodleAuth

async def test_extraction():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()
        
        url = "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45694" # Unidade 1 :: Aula 2
        print("Navigating to:", url)
        await page.goto(url, wait_until="networkidle")
        
        # Check attempt button
        btn = page.locator("form[action*='startattempt.php'] button, button:has-text('tentativa'), a:has-text('tentativa')").first
        if await btn.count() > 0:
            print("Clicking attempt button...")
            await btn.click()
            await page.wait_for_load_state("networkidle")
            print("Current URL:", page.url)
            
            # Run DOM extractor
            questions = await page.evaluate('''() => {
                const questions = [];
                const qNodes = document.querySelectorAll(".que");
                let globalInputIdx = 1;

                qNodes.forEach((q, qIndex) => {
                    const noText = q.querySelector(".info .no") ? q.querySelector(".info .no").innerText.trim() : `Questão ${qIndex + 1}`;
                    const contentEl = q.querySelector(".content") || q;
                    
                    const clone = contentEl.cloneNode(true);
                    const inputs = clone.querySelectorAll("input[type='text'], textarea");
                    const inputMap = [];

                    inputs.forEach(inp => {
                        const token = `[[CAMPO_${globalInputIdx}]]`;
                        inputMap.push({
                            token: token,
                            name: inp.getAttribute("name"),
                            id: inp.getAttribute("id"),
                            index: globalInputIdx
                        });
                        const textNode = document.createTextNode(` ${token} `);
                        inp.parentNode.replaceChild(textNode, inp);
                        globalInputIdx++;
                    });

                    questions.push({
                        qIndex: qIndex + 1,
                        qNumberText: noText,
                        fullTextWithTokens: clone.innerText.trim(),
                        inputsCount: inputMap.length,
                        inputs: inputMap
                    });
                });

                return questions;
            }''')
            
            print(f"Extracted {len(questions)} questions:")
            for q in questions:
                print(f"Question {q['qIndex']} ({q['qNumberText']}): {q['inputsCount']} inputs")
                print("Text snippet:", q['fullTextWithTokens'][:200])
                print("---")
        else:
            print("No attempt button found!")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_extraction())
