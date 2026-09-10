import json
import sys
import urllib.request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOKEN = "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"
PAGE_ID = "17db4e452b43449a9ca266065840f909"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

def get_children(block_id, depth=0):
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for b in data.get("results", []):
                b_type = b.get("type")
                indent = "  " * depth
                text = ""
                if b_type in ["paragraph", "heading_1", "heading_2", "heading_3", "callout", "to_do"]:
                    text = "".join([x.get("plain_text", "") for x in b.get(b_type, {}).get("rich_text", [])])
                elif b_type in ["child_page", "child_database"]:
                    text = b.get(b_type, {}).get("title", "")
                elif b_type == "link_to_page":
                    text = str(b.get("link_to_page", {}))
                print(f"{indent}[{b_type}] {text}")
                if b_type == "table":
                    rows_url = f"https://api.notion.com/v1/blocks/{b.get('id')}/children"
                    rows_req = urllib.request.Request(rows_url, headers=HEADERS)
                    with urllib.request.urlopen(rows_req) as r_resp:
                        r_data = json.loads(r_resp.read().decode("utf-8"))
                        for row in r_data.get("results", []):
                            cells = row.get("table_row", {}).get("cells", [])
                            line = ["".join([x.get("plain_text", "") for x in c]) for c in cells]
                            print(f"{indent}  ROW: {' | '.join(line)}")

                if b.get("has_children") and depth < 3:
                    get_children(b.get("id"), depth + 1)

    except Exception as e:
        print(f"Error reading block {block_id}: {e}")

print("=== Estrutura da Página Minha Central ===")
get_children(PAGE_ID)
