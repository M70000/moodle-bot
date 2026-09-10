import json
import os
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
CENTRAL_PAGE = "17db4e452b43449a9ca266065840f909"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

def notion_request(endpoint, method="GET", body=None):
    url = f"https://api.notion.com/v1/{endpoint}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Error {endpoint}: {e}")
        return {}

# 1. Busca todas as tarefas que tenham 'Lista' ou sejam de agosto
print("=== Buscando tarefas de 'Lista' ou 'Eletromag' ===")
query_res = notion_request(f"databases/{TASKS_DB}/query", method="POST", body={
    "filter": {
        "or": [
            {"property": "Task Title", "title": {"contains": "Lista"}},
            {"property": "Task Title", "title": {"contains": "lista"}},
            {"property": "Task Title", "title": {"contains": "Eletromag"}}
        ]
    }
})

for t in query_res.get("results", []):
    props = t.get("properties", {})
    title = "".join([x.get("plain_text", "") for x in props.get("Task Title", {}).get("title", [])])
    tarefa_type = props.get("tarefa", {}).get("select", {})
    tarefa_name = tarefa_type.get("name") if tarefa_type else "None"
    data = props.get("data", {}).get("date", {})
    progresso = props.get("progresso", {}).get("status", {}).get("name")
    curso_rel = props.get("curso", {}).get("relation", [])
    print(f"• Título: '{title}' | Tipo: {tarefa_name} | Data: {data} | Progresso: {progresso} | Cursos: {[r.get('id') for r in curso_rel]}")

# 2. Inspeciona blocos da página central para ver se há tabela de horários/agenda semanal
print("\n=== Inspecionando blocos da página central 'Minha central' ===")
blocks = notion_request(f"blocks/{CENTRAL_PAGE}/children")
for b in blocks.get("results", []):
    b_type = b.get("type")
    content = ""
    if b_type.startswith("heading"):
        content = "".join([x.get("plain_text", "") for x in b.get(b_type, {}).get("rich_text", [])])
    elif b_type == "child_database":
        content = b.get("child_database", {}).get("title")
    elif b_type == "child_page":
        content = b.get("child_page", {}).get("title")
    print(f"[{b_type}] {content} (ID: {b.get('id')})")
