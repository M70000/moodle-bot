import json
import sys
import urllib.request
import urllib.error

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOKEN = "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"
TASKS_DB = "00e5c698-5139-4b4c-9cac-db04bfc22c4b"
ELETROMAG_COURSE_ID = "d6266a51-585d-48ba-a9a5-84cb7cc55ca1"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

def create_notion_task(title, date_str, task_id_val, notes_val, gabarito_val=""):
    url = "https://api.notion.com/v1/pages"

    page_children = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": f"Material do Moodle sincronizado pelo assistente.\nArquivo: {title.replace('Lista de Estudo ', 'Lista_')}.pdf" + (f"\nGabarito: {gabarito_val}" if gabarito_val else "")
                        }
                    }
                ],
                "icon": {"emoji": "📚"}
            }
        },
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": "Etapas de Estudo"}}]
            }
        },
        {
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": "Revisar anotações da matéria de Eletromagnetismo"}}],
                "checked": False
            }
        },
        {
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": "Resolver questões da lista"}}],
                "checked": False
            }
        }
    ]

    if gabarito_val:
        page_children.append({
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": f"Conferir resolução com {gabarito_val}"}}],
                "checked": False
            }
        })

    payload = {
        "parent": {"database_id": TASKS_DB},
        "properties": {
            "Task Title": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "tarefa": {
                "select": {"name": "TAREFA✅"}
            },
            "data": {
                "date": {"start": date_str}
            },
            "progresso": {
                "status": {"name": "não iniciado"}
            },
            "domínio": {
                "status": {"name": "à fazer"}
            },
            "curso": {
                "relation": [{"id": ELETROMAG_COURSE_ID}]
            },
            "notas": {
                "rich_text": [{"type": "text", "text": {"content": notes_val}}]
            },
            "Task ID": {
                "rich_text": [{"type": "text", "text": {"content": task_id_val}}]
            }
        },
        "children": page_children
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"✔ Criado com sucesso: {title} (ID da página: {data.get('id')})")
            return data
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")
        print(f"✖ Erro HTTP {e.code} ao criar {title}: {err}")
        return None
    except Exception as e:
        print(f"✖ Erro ao criar {title}: {e}")
        return None

print("=== Criando Listas de Estudo 3, 4 e 5 no Notion ===")

# 1. Lista 3: Sexta-feira (2026-09-11)
create_notion_task(
    title="Lista de Estudo 3",
    date_str="2026-09-11",
    task_id_val="moodle_study_eletromag_lista_3",
    notes_val="Moodle: Lista_3.pdf (Gabarito lista 3.pdf disponível)",
    gabarito_val="Gabarito lista 3.pdf"
)

# 2. Lista 4: Sábado (2026-09-12)
create_notion_task(
    title="Lista de Estudo 4",
    date_str="2026-09-12",
    task_id_val="moodle_study_eletromag_lista_4",
    notes_val="Moodle: Lista_4.pdf (Gabarito lista 4.pdf disponível)",
    gabarito_val="Gabarito lista 4.pdf"
)

# 3. Lista 5: Domingo (2026-09-13)
create_notion_task(
    title="Lista de Estudo 5",
    date_str="2026-09-13",
    task_id_val="moodle_study_eletromag_lista_5",
    notes_val="Moodle: Lista_5.pdf",
    gabarito_val=""
)
