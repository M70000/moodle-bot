import json
import urllib.request
import sys
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOKEN = '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'
CHECKLIST_BLOCK_ID = '25cd128a-26fe-49ac-8ab0-a895f1e0858d'
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Notion-Version': '2022-06-28',
    'Content-Type': 'application/json'
}

def notion_req(endpoint, method="GET", data=None):
    url = f"https://api.notion.com/v1/{endpoint}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, headers=HEADERS, data=body, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

print("=== 1. Buscando filhos atuais de tarefas do dia ===")
children = notion_req(f"blocks/{CHECKLIST_BLOCK_ID}/children")
print(f"Total filhos: {len(children.get('results', []))}")

to_do_blocks = []
for b in children.get("results", []):
    b_id = b.get("id")
    b_type = b.get("type")
    print(f"Bloco: {b_id} | Tipo: {b_type}")
    if b_type == "to_do":
        to_do_blocks.append(b_id)

print(f"\n=== 2. Removendo to_do antigos ({len(to_do_blocks)} blocos) mantendo o botão ===")
for b_id in to_do_blocks:
    try:
        notion_req(f"blocks/{b_id}", method="DELETE")
        print(f"✔ Removido to_do: {b_id}")
    except Exception as e:
        print(f"✖ Erro ao remover {b_id}: {e}")

print("\n=== 3. Inserindo novas tarefas do dia ===")
sample_tasks = [
    "Revisar anotações de Eletromagnetismo (Ondas EM)",
    "Lista de Estudo 3 - Questões 1 a 4",
    "Estudo de Química Geral B",
    "Conferir pendências no Moodle"
]

new_children = []
for t in sample_tasks:
    new_children.append({
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": [{"type": "text", "text": {"content": t}}],
            "checked": False
        }
    })

res = notion_req(f"blocks/{CHECKLIST_BLOCK_ID}/children", method="PATCH", data={"children": new_children})
print(f"✔ Tarefas inseridas com sucesso! Novos blocos criados: {len(res.get('results', []))}")
