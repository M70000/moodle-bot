import json
import urllib.request
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOKEN = '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'
TASKS_DB = '00e5c698-5139-4b4c-9cac-db04bfc22c4b'
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}

req = urllib.request.Request(f'https://api.notion.com/v1/databases/{TASKS_DB}/query', headers=HEADERS, data=json.dumps({'page_size': 50}).encode())
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    print(f"Total tasks found: {len(data.get('results', []))}")
    for t in data.get('results', []):
        props = t.get('properties', {})
        title = ''.join([x.get('plain_text', '') for x in props.get('Task Title', {}).get('title', [])])
        prog = props.get('progresso', {}).get('status', {}).get('name')
        date = props.get('data', {}).get('date', {})
        t_sel = props.get('tarefa')
        cat = t_sel.get('select', {}).get('name') if t_sel and t_sel.get('select') else None
        print(f"• '{title}' | Prog: {prog} | Data: {date} | Cat: {cat}")
