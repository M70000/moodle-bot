"""Utilitário de Atualização Automática para Instalações via Download ZIP (Sem Git).

Permite que usuários que baixaram o projeto como .zip do GitHub atualizem
o sistema para a versão mais recente com 1 clique (atualizar.bat), preservando
estritamente todas as configurações pessoais (.env), cookies e tarefas.
"""

import io
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Raiz do projeto (c:\moodle-bot)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

GITHUB_REPO = "M70000/moodle-bot"
GITHUB_ZIP_URL = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"

# Arquivos e diretórios que NUNCA devem ser sobrescritos durante a atualização
PRESERVED_EXACT_FILES: Set[str] = {
    ".env",
    "storage/state.json",
    "storage/cookies/session.json",
    "storage/cookies/cookies.json",
}

PRESERVED_DIR_PREFIXES: Tuple[str, ...] = (
    "storage/cookies/",
    "storage/materials/",
    "storage/submissions/",
    ".venv/",
    "venv/",
    ".git/",
    ".vscode/",
    ".idea/",
    "scratch/",
    "logs/",
    "__pycache__/",
    ".agents/",
    ".claude/",
    ".cloud/",
)


def should_preserve(rel_path_str: str) -> bool:
    """Verifica se um caminho relativo deve ser preservado contra sobrescrita."""
    norm = rel_path_str.replace("\\", "/").strip("/")
    if not norm:
        return True

    if norm in PRESERVED_EXACT_FILES:
        return True

    # Preserva variações de .env (.env.local, .env.backup), mas permite atualizar .env.example
    if norm.startswith(".env.") and norm != ".env.example":
        return True

    if norm == "GEMINI.md":
        return True

    for prefix in PRESERVED_DIR_PREFIXES:
        if norm.startswith(prefix) or f"/{prefix}" in norm:
            return True

    return False


def download_latest_zip(url: str = GITHUB_ZIP_URL) -> bytes:
    """Baixa o arquivo .zip da branch principal do GitHub."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MoodleBot-Updater/1.0",
            "Accept": "application/vnd.github.v3+json, application/zip, */*",
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"GitHub retornou status HTTP {resp.status}")
        return resp.read()


def apply_zip_update(zip_bytes: bytes, dest_root: Path = PROJECT_ROOT) -> Dict[str, int]:
    """Aplica o conteúdo do ZIP no diretório de destino, preservando dados do usuário."""
    stats = {"updated": 0, "created": 0, "preserved": 0}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        namelist = zf.namelist()
        if not namelist:
            raise ValueError("O arquivo ZIP recebido do GitHub está vazio.")

        # O GitHub empacota como <repo>-<branch>/ (ex: moodle-bot-main/)
        root_prefix = namelist[0].split("/")[0] + "/"

        for member in zf.infolist():
            raw_name = member.filename
            if not raw_name.startswith(root_prefix):
                continue

            rel_path = raw_name[len(root_prefix):]
            if not rel_path or rel_path.endswith("/"):
                # Diretório
                target_dir = dest_root / rel_path
                target_dir.mkdir(parents=True, exist_ok=True)
                continue

            if should_preserve(rel_path):
                stats["preserved"] += 1
                continue

            target_file = dest_root / rel_path
            target_file.parent.mkdir(parents=True, exist_ok=True)

            is_new = not target_file.exists()
            content = zf.read(raw_name)

            # Garante que scripts de lote/PowerShell sempre usem CRLF para evitar erros no cmd.exe
            if rel_path.lower().endswith((".bat", ".cmd", ".ps1")):
                content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

            target_file.write_bytes(content)

            if is_new:
                stats["created"] += 1
            else:
                stats["updated"] += 1

    return stats


def main():
    print("=" * 60)
    print("  Moodle AI Assistant - Atualizador de Versao (Modo ZIP)")
    print("=" * 60)
    print()
    print(f"Repositório: https://github.com/{GITHUB_REPO}")
    print()

    print("[1/3] Conectando ao GitHub e baixando a versao mais recente...")
    try:
        t0 = time.time()
        zip_bytes = download_latest_zip()
        kb = len(zip_bytes) / 1024
        print(f"      Download concluido ({kb:.1f} KB em {time.time() - t0:.1f}s).")
    except Exception as e:
        print(f"[ERRO] Falha ao baixar atualizacao do GitHub: {e}")
        print("Verifique sua conexao com a internet ou se o repositorio esta acessivel.")
        sys.exit(1)

    print()
    print("[2/3] Aplicando atualizacoes nos arquivos do assistente...")
    try:
        stats = apply_zip_update(zip_bytes)
        print(f"      Arquivos atualizados: {stats['updated']}")
        print(f"      Novos arquivos adicionados: {stats['created']}")
        print(f"      Arquivos/dados preservados intactos: {stats['preserved']}")
    except Exception as e:
        print(f"[ERRO] Falha ao extrair e aplicar atualizacao: {e}")
        sys.exit(1)

    print()
    print("[OK] Arquivos do assistente sincronizados com sucesso!")
    sys.exit(0)


if __name__ == "__main__":
    main()
