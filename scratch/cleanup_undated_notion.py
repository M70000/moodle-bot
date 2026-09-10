import json
import sys
import urllib.request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOKEN = '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'
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}

pages_to_delete = [
    '3d7d5d0f-025a-8170-9f76-f97ebc3ef61d',
    '3d7d5d0f-025a-811e-828e-fc0bd61a87aa',
    '3d7d5d0f-025a-81d7-8e94-e9f156494871',
    '3d7d5d0f-025a-8198-bb60-d66ba5a26f4a',
    '3d7d5d0f-025a-81c7-882a-feb09c722a55',
    '3d7d5d0f-025a-81b8-937d-ca10facc3120',
    '3d7d5d0f-025a-81ad-9a1a-ea89fa2b8563',
    '3d7d5d0f-025a-8110-afac-d899f4de9f58'
]

for pid in pages_to_delete:
    try:
        req = urllib.request.Request(f'https://api.notion.com/v1/blocks/{pid}', headers=HEADERS, method='DELETE')
        urllib.request.urlopen(req)
        print('Removido do Notion com sucesso:', pid)
    except Exception as e:
        print('Erro ao remover:', pid, e)
