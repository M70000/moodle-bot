"""Testes para o utilitário de atualização via ZIP (tools/update_zip.py)."""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.update_zip import apply_zip_update, should_preserve


class TestZipUpdater(unittest.TestCase):
    def test_should_preserve_critical_files(self):
        # Arquivos estritamente preservados
        self.assertTrue(should_preserve(".env"))
        self.assertTrue(should_preserve(".env.local"))
        self.assertTrue(should_preserve(".env.backup"))
        self.assertTrue(should_preserve("storage/state.json"))
        self.assertTrue(should_preserve("storage/cookies/session.json"))
        self.assertTrue(should_preserve("storage/materials/calculo/slide1.pdf"))
        self.assertTrue(should_preserve("storage/submissions/relatorio.pdf"))
        self.assertTrue(should_preserve(".venv/Scripts/python.exe"))
        self.assertTrue(should_preserve(".git/config"))
        self.assertTrue(should_preserve("GEMINI.md"))
        self.assertTrue(should_preserve(".agents/skills/test.md"))
        self.assertTrue(should_preserve(".claude/skills/test.md"))

    def test_should_not_preserve_code_files(self):
        # Arquivos que DEVEM ser atualizados normalmente
        self.assertFalse(should_preserve(".env.example"))
        self.assertFalse(should_preserve("README.md"))
        self.assertFalse(should_preserve("requirements.txt"))
        self.assertFalse(should_preserve("src/notifier/discord_bot.py"))
        self.assertFalse(should_preserve("config/settings.py"))
        self.assertFalse(should_preserve("atualizar.bat"))
        self.assertFalse(should_preserve("iniciar.bat"))

    def test_apply_zip_update(self):
        # Cria um arquivo zip em memória simulando o empacotamento do GitHub
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("moodle-bot-main/README.md", "Novo README Atualizado")
            zf.writestr("moodle-bot-main/.env", "DISCORD_BOT_TOKEN=token_novo_do_zip")
            zf.writestr("moodle-bot-main/.env.example", "DISCORD_BOT_TOKEN=exemplo_atualizado")
            zf.writestr("moodle-bot-main/storage/state.json", '{"tasks": []}')
            zf.writestr("moodle-bot-main/src/novo_modulo.py", "print('hello')")

        buf.seek(0)
        zip_bytes = buf.read()

        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir)
            # Cria estado pré-existente
            (dest / ".env").write_text("DISCORD_BOT_TOKEN=meu_token_secreto_local", encoding="utf-8")
            (dest / "storage").mkdir(parents=True, exist_ok=True)
            (dest / "storage" / "state.json").write_text('{"meu_estado_antigo": 123}', encoding="utf-8")
            (dest / "README.md").write_text("README Antigo", encoding="utf-8")

            stats = apply_zip_update(zip_bytes, dest_root=dest)

            # O README deve ter sido atualizado
            self.assertEqual((dest / "README.md").read_text(encoding="utf-8"), "Novo README Atualizado")
            # O novo arquivo deve ter sido criado
            self.assertTrue((dest / "src" / "novo_modulo.py").exists())
            # O .env DEVE TER SIDO PRESERVADO intacto
            self.assertEqual(
                (dest / ".env").read_text(encoding="utf-8"),
                "DISCORD_BOT_TOKEN=meu_token_secreto_local"
            )
            # O state.json DEVE TER SIDO PRESERVADO intacto
            self.assertEqual(
                (dest / "storage" / "state.json").read_text(encoding="utf-8"),
                '{"meu_estado_antigo": 123}'
            )
            # O .env.example DEVE ter sido atualizado
            self.assertEqual(
                (dest / ".env.example").read_text(encoding="utf-8"),
                "DISCORD_BOT_TOKEN=exemplo_atualizado"
            )

            self.assertEqual(stats["updated"], 1)  # README.md
            self.assertEqual(stats["created"], 2)  # .env.example e src/novo_modulo.py
            self.assertEqual(stats["preserved"], 2)  # .env e storage/state.json


if __name__ == "__main__":
    unittest.main()
