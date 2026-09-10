import json
import urllib.request
import sys
from datetime import datetime, timedelta

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOKEN = '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'
CHECKLIST_BLOCK_ID = '25cd128a-26fe-49ac-8ab0-a895f1e0858d'
SCHEDULE_TABLE_ID = '2a9222dd-474a-4c40-9b96-a548f2c9ec11'
TASKS_DB = '00e5c698-5139-4b4c-9cac-db04bfc22c4b'

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

today = datetime.now()
today_str = today.strftime("%Y-%m-%d")
tomorrow_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
dias_semana = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
dia_nome = dias_semana[today.weekday()]

print(f"Data atual: {today_str} ({dia_nome})")

# 1. Busca compromissos da Agenda Semanal para o dia de hoje
print("\n=== 1. Consultando Agenda Semanal ===")
routine_items = []
try:
    rows_data = notion_req(f"blocks/{SCHEDULE_TABLE_ID}/children?page_size=100")
    results = rows_data.get("results", [])
    if results:
        header_cells = results[0].get("table_row", {}).get("cells", [])
        headers = ["".join([x.get("plain_text", "") for x in c]).lower().strip() for c in header_cells]
        col_idx = None
        for i, h in enumerate(headers):
            if dia_nome in h:
                col_idx = i
                break

        if col_idx is not None:
            for row in results[1:]:
                cells = row.get("table_row", {}).get("cells", [])
                if len(cells) > col_idx:
                    h_time = "".join([x.get("plain_text", "") for x in cells[0]]).strip()
                    act = "".join([x.get("plain_text", "") for x in cells[col_idx]]).strip()
                    if act and not any(ign in act.lower() for ign in ["dormir", "banho", "café", "almoço", "jantar", "lanche", "pós-treino"]):
                        emoji = "🎓" if any(a in act.lower() for a in ["química", "cálculo", "eletromag", "estatística", "mandarim"]) else ("🏋️" if "academia" in act.lower() else "📌")
                        routine_items.append(f"{emoji} {act} ({h_time})")
except Exception as e:
    print(f"Erro ao ler agenda: {e}")

print(f"Itens da rotina do dia encontrados ({len(routine_items)}):")
for r in routine_items:
    print(f"  • {r}")

# 2. Busca tarefas no banco de dados de tarefas
print("\n=== 2. Consultando Tarefas Pendentes do Notion ===")
task_items = []
try:
    query_body = {
        "filter": {
            "property": "progresso", "status": {"does_not_equal": "completo"}
        },
        "sorts": [{"property": "data", "direction": "ascending"}],
        "page_size": 40
    }
    q_data = notion_req(f"databases/{TASKS_DB}/query", method="POST", data=query_body)
    for t in q_data.get("results", []):
        props = t.get("properties", {})
        title = "".join([x.get("plain_text", "") for x in props.get("Task Title", {}).get("title", [])]).strip()
        date_obj = props.get("data", {}).get("date") or {}
        start_d = (date_obj.get("start") or "")[:10]
        
        # Tarefas de hoje, amanhã ou atrasadas
        if start_d:
            if start_d == today_str:
                task_items.append(f"⭐ [Hoje] {title}")
            elif start_d < today_str and "estudo" in title.lower():
                task_items.append(f"⚠️ [Pendente] {title}")
            elif start_d == tomorrow_str:
                task_items.append(f"⏳ [Amanhã] {title}")
except Exception as e:
    print(f"Erro ao buscar tarefas: {e}")

print(f"Tarefas prioritárias encontradas ({len(task_items)}):")
for t in task_items:
    print(f"  • {t}")

# 3. Consolidação final
final_tasks = []
# Adiciona primeiro as tarefas acadêmicas prioritárias
for t in task_items[:4]:
    final_tasks.append(t)
# Adiciona os blocos da rotina do dia
for r in routine_items:
    final_tasks.append(r)

print(f"\n=== Total de itens para a checklist de hoje: {len(final_tasks)} ===")
for f in final_tasks:
    print(f"  [ ] {f}")
