import json
import urllib.request
import urllib.error

TOKEN = "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"
PAGE_ID = "17db4e452b43449a9ca266065840f909"

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
    except urllib.error.HTTPError as e:
        err_content = e.read().decode("utf-8")
        print(f"HTTP Error {e.code}: {err_content}")
        return {"error": e.code, "message": err_content}
    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}

import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

print("=== 1. Buscando todas as databases acessíveis à integração ===")
search_res = notion_request("search", method="POST", body={"filter": {"value": "database", "property": "object"}})
results = search_res.get("results", [])
print(f"Databases encontradas: {len(results)}")

dump = []
for db in results:
    title_list = db.get("title", [])
    title = "".join([t.get("plain_text", "") for t in title_list]) or "Sem título"
    db_id = db.get("id")
    print(f"\n--- Database: '{title}' (ID: {db_id}) ---")
    props = db.get("properties", {})
    prop_summary = {}
    for p_name, p_info in props.items():
        p_type = p_info.get("type")
        extra = ""
        if p_type == "select":
            opts = [o.get("name") for o in p_info.get("select", {}).get("options", [])]
            extra = f" (Opções: {opts})"
            prop_summary[p_name] = {"type": p_type, "options": opts}
        elif p_type == "multi_select":
            opts = [o.get("name") for o in p_info.get("multi_select", {}).get("options", [])]
            extra = f" (Opções: {opts})"
            prop_summary[p_name] = {"type": p_type, "options": opts}
        elif p_type == "status":
            opts = [o.get("name") for o in p_info.get("status", {}).get("options", [])]
            extra = f" (Status: {opts})"
            prop_summary[p_name] = {"type": p_type, "options": opts}
        elif p_type == "relation":
            rel_db = p_info.get("relation", {}).get("database_id")
            extra = f" (Relation to DB: {rel_db})"
            prop_summary[p_name] = {"type": p_type, "relation_db": rel_db}
        else:
            prop_summary[p_name] = {"type": p_type}
        print(f"  • {p_name}: [{p_type}]{extra}")
    dump.append({"id": db_id, "title": title, "properties": prop_summary})

print("\n=== 2. Consultando cursos cadastrados na database 'central' ===")
courses_res = notion_request("databases/751117de-c4d2-468c-9b46-571c036969b1/query", method="POST", body={"page_size": 10})
courses = courses_res.get("results", [])
print(f"Cursos cadastrados: {len(courses)}")
for c in courses:
    props = c.get("properties", {})
    name_list = props.get("Nome", {}).get("title", [])
    name = "".join([t.get("plain_text", "") for t in name_list]) or "Sem nome"
    code_list = props.get("course code", {}).get("rich_text", [])
    code = "".join([t.get("plain_text", "") for t in code_list])
    abbrev_list = props.get("abreviation", {}).get("rich_text", [])
    abbrev = "".join([t.get("plain_text", "") for t in abbrev_list])
    print(f"  • ID: {c.get('id')} | Nome: '{name}' | Código: '{code}' | Abrev: '{abbrev}'")

print("\n=== 3. Consultando últimas 5 tarefas na database 'à fazer' ===")
tasks_res = notion_request("databases/00e5c698-5139-4b4c-9cac-db04bfc22c4b/query", method="POST", body={"page_size": 5})
tasks = tasks_res.get("results", [])
print(f"Tarefas encontradas: {len(tasks)}")
for t in tasks:
    props = t.get("properties", {})
    title_list = props.get("Task Title", {}).get("title", [])
    t_title = "".join([x.get("plain_text", "") for x in title_list])
    t_type = props.get("tarefa", {}).get("select", {})
    t_type_name = t_type.get("name") if t_type else "None"
    t_prog = props.get("progresso", {}).get("status", {})
    t_prog_name = t_prog.get("name") if t_prog else "None"
    t_data = props.get("data", {}).get("date", {})
    t_due = props.get("Task Due", {}).get("date", {})
    t_sync = props.get("Sync Status", {}).get("select", {})
    t_sync_name = t_sync.get("name") if t_sync else "None"
    t_rel = props.get("curso", {}).get("relation", [])
    rel_ids = [r.get("id") for r in t_rel]
    print(f"  • Tarefa: '{t_title}' | Tipo: {t_type_name} | Progresso: {t_prog_name} | Data: {t_data} | Task Due: {t_due} | Sync: {t_sync_name} | Curso Rel: {rel_ids}")


