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
PAGE_ID = '17db4e452b43449a9ca266065840f909'
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Notion-Version': '2022-06-28'}

def find_blocks(b_id):
    url = f'https://api.notion.com/v1/blocks/{b_id}/children?page_size=100'
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        for b in data.get('results', []):
            b_type = b.get('type')
            text = ''
            if 'rich_text' in b.get(b_type, {}):
                text = ''.join([x.get('plain_text', '') for x in b[b_type]['rich_text']])
            if 'tarefas do dia' in text.lower():
                print('Found tarefas do dia block!')
                print(json.dumps(b, indent=2))
                # Get children of this block
                block_id = b.get('id')
                c_url = f'https://api.notion.com/v1/blocks/{block_id}/children'
                c_req = urllib.request.Request(c_url, headers=HEADERS)
                with urllib.request.urlopen(c_req) as c_resp:
                    c_data = json.loads(c_resp.read().decode('utf-8'))
                    print('Children of tarefas do dia:')
                    print(json.dumps(c_data, indent=2))
            if b.get('has_children'):
                find_blocks(b.get('id'))

find_blocks(PAGE_ID)
