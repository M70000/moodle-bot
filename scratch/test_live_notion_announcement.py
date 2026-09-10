import asyncio
import sys
from src.notifier.notion_client import notion_client

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

async def test_live():
    print("=== Testando Adição no Notion com Anúncio Detalhado no Discord ===")
    res = await notion_client.create_task(
        title="Estudo: Ondas Eletromagnéticas em Meios Condutores",
        date_str="2026-09-15",
        category="TAREFA✅",
        course_name="Eletromagnetismo",
        task_id_val="test_notion_announcement_live_1",
        notes_val="Moodle UFMG: Exercícios de fixação sobre atenuação e profundidade de penetração.",
        details="Tópico avançado cobrindo o vetor de Poynting, efeito pelicular (skin effect) e equações de onda em bons condutores.",
        steps=[
            "Revisar teoria sobre a profundidade de penetração (skin depth)",
            "Calcular as perdas por efeito Joule na fronteira",
            "Resolver os exercícios propostos no Moodle",
            "Conferir com as anotações do professor"
        ],
        notify_discord=True
    )
    print("Resultado da criação:", res)

if __name__ == "__main__":
    asyncio.run(test_live())
