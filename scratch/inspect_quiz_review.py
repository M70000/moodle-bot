import asyncio
from playwright.async_api import async_playwright
from src.auth.moodle_auth import MoodleAuth

async def inspect_review():
    auth = MoodleAuth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=auth.cookies_path)
        page = await context.new_page()
        
        # Open Unidade 1 :: Aula 1 (id=45693)
        url = "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45693"
        print("Navigating to:", url)
        await page.goto(url, wait_until="networkidle")
        
        # Find review links
        links = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll("a")).filter(a => a.href.includes("review.php")).map(a => ({
                text: a.innerText.trim(),
                href: a.href
            }));
        }''')
        print("Review links:", links)
        
        if links:
            rev_url = links[0]["href"]
            print("Navigating to review:", rev_url)
            await page.goto(rev_url, wait_until="networkidle")
            
            # Inspect questions structure on review page (Moodle standard questions)
            questions = await page.evaluate('''() => {
                const qNodes = Array.from(document.querySelectorAll(".que"));
                return qNodes.map(q => {
                    const qNo = q.querySelector(".info .no") ? q.querySelector(".info .no").innerText.trim() : "";
                    const qStatus = q.querySelector(".info .state") ? q.querySelector(".info .state").innerText.trim() : "";
                    const qText = q.querySelector(".content .qtext") ? q.querySelector(".content .qtext").innerText.trim() : "";
                    
                    // Inputs
                    const inputs = Array.from(q.querySelectorAll("input, select, textarea")).map(el => ({
                        tag: el.tagName,
                        type: el.type,
                        name: el.name,
                        value: el.value,
                        checked: el.checked
                    }));
                    
                    // Answers/Choices
                    const choices = Array.from(q.querySelectorAll(".answer div, .answer table tr")).map(c => c.innerText.trim());
                    
                    return {
                        id: q.id,
                        qNo,
                        qStatus,
                        qText: qText.substring(0, 100),
                        inputs,
                        choices
                    };
                });
            }''')
            print(f"Found {len(questions)} questions on review page:")
            for q in questions:
                print(q)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(inspect_review())
