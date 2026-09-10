import json
import urllib.request
from datetime import datetime

TOKEN = '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'
TASKS_DB = '00e5c698-5139-4b4c-9cac-db04bfc22c4b'
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Notion-Version': '2022-06-28',
    'Content-Type': 'application/json'
}

req = urllib.request.Request(
    f'https://api.notion.com/v1/databases/{TASKS_DB}/query',
    headers=HEADERS,
    data=json.dumps({
        'filter': {'property': 'progresso', 'status': {'does_not_equal': 'completo'}},
        'sorts': [{'property': 'data', 'direction': 'ascending'}]
    }).encode('utf-8')
)

res = json.loads(urllib.request.urlopen(req).read().decode('utf-8'))
for r in res.get('results', []):
    t_list = r['properties']['Task Title']['title']
    title = t_list[0]['plain_text'] if t_list else 'Sem titulo'
    d_obj = r['properties'].get('data', {}).get('date') or {}
    start_d = d_obj.get('start')
    print(f"{title:40} | Data: {start_d}")
