import asyncio
from pathlib import Path
from src.scraper.moodle_scraper import Assignment
from src.scraper.moodle_quiz import MoodleQuizAutomator
from src.solver.gemini_solver import GeminiSolver

async def test_live_quiz_solver():
    automator = MoodleQuizAutomator()
    solver = GeminiSolver()
    
    quiz_url = "https://virtual.ufmg.br/20262/mod/quiz/view.php?id=45694"
    assign = Assignment(
        id="45694",
        course_id="",
        course_name="2026_2 - INGLÊS INSTRUMENTAL I - METATURMA",
        title="Unidade 1 :: Aula 2",
        url=quiz_url,
        description="",
        activity_type="quiz"
    )
    
    print("1. Extracting live quiz structure from Moodle...")
    ext_res = await automator.inspect_and_extract_quiz(quiz_url)
    if not ext_res.get("success"):
        print("Extraction failed:", ext_res.get("error"))
        return
        
    print(f"Extraction success! Total inputs: {ext_res['total_inputs']}")
    
    print("2. Solving with live context using GeminiSolver...")
    ref_file = Path("storage/submissions/temp_uploads/Ingles_instrumental_1_-_RESPOSTAS.pdf")
    extra_files = [ref_file] if ref_file.exists() else None
    
    draft = await solver.solve_quiz_with_live_context(
        assignment=assign,
        questions_data=ext_res["questions"],
        extra_context_files=extra_files
    )
    
    print("\n--- STRUCTURED ANSWERS ---")
    print(draft.structured_answers)
    
    print("\n--- CHECK FOR SLASHES IN ANSWERS ---")
    has_slash = False
    if draft.structured_answers:
        for item in draft.structured_answers:
            val = item.get("value", "")
            if "/" in str(val):
                print(f"WARNING: Slash found in {item['key']}: {val}")
                has_slash = True
        if not has_slash:
            print("✔ ALL ANSWERS ARE CLEAN SINGLE WORDS (NO SLASHES)!")
            
    print("\n--- FIRST 600 CHARS OF CLEAN MARKDOWN ---")
    print(draft.full_markdown[:600])

if __name__ == "__main__":
    asyncio.run(test_live_quiz_solver())
