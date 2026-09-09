"""Motor de Submissão Automatizada de Tarefas no Moodle / UFMG Virtual.

Executa a entrega do arquivo em PDF ou texto online em segundo plano (headless),
garantindo salvamento e validação de status no Moodle.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from rich.console import Console
from rich.panel import Panel

from config.settings import settings
from src.auth.moodle_auth import MoodleAuth
from src.scraper.moodle_scraper import Assignment

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()


class MoodleSubmitter:
    """Responsável por executar a submissão formal de atividades no Moodle."""

    def __init__(self, auth: Optional[MoodleAuth] = None):
        self.auth = auth or MoodleAuth()

    async def submit_assignment(
        self,
        assignment_url: str,
        file_path: Path,
        text_content: Optional[str] = None,
        dry_run: bool = False
    ) -> Tuple[bool, str]:
        """Acessa o Moodle e submete o arquivo para a tarefa indicada.

        Args:
            assignment_url: URL da tarefa (ex: https://virtual.ufmg.br/20262/mod/assign/view.php?id=102068)
            file_path: Caminho do arquivo a ser enviado (ex: PDF da resolução)
            text_content: Texto online complementar (se a tarefa aceitar envio de texto)
            dry_run: Se True, apenas navega e valida os botões sem confirmar a entrega.

        Returns:
            Tuple[bool, str]: (sucesso, mensagem_ou_erro)
        """
        if not file_path.exists():
            return False, f"Arquivo de submissão não encontrado: {file_path}"

        if not self.auth.session_exists:
            return False, "Sessão do Moodle não encontrada. Execute login primeiro."

        console.print(
            Panel.fit(
                f"[bold cyan]Submissão no Moodle UFMG[/bold cyan]\n"
                f"• URL: [underline]{assignment_url}[/underline]\n"
                f"• Arquivo: [bold]{file_path.name}[/bold] ({file_path.stat().st_size // 1024} KB)\n"
                f"• Modo: [{'yellow]Simulação (Dry-run)' if dry_run else 'green]Envio Real Definitivo'}[/]",
                title="[bold yellow]Iniciando Entrega Automática[/bold yellow]",
                border_style="cyan"
            )
        )

        async with async_playwright() as p:
            browser, context = await self.auth.get_authenticated_context(p, headless=True)
            try:
                page = await context.new_page()
                console.print(f"[dim]Acessando {assignment_url}...[/dim]")
                await page.goto(assignment_url, wait_until="domcontentloaded", timeout=30000)

                # 1. Proteção estrita: NUNCA usar 'Editar envio' para evitar sobrescrever trabalhos já entregues
                has_existing_submission = await page.query_selector(
                    "button:has-text('Editar envio'), input[value*='Editar envio'], a:has-text('Editar envio')"
                )
                if has_existing_submission:
                    return False, "Bloqueado por segurança: Esta atividade já possui um envio anterior no Moodle. O assistente nunca sobrescreve trabalhos já entregues."

                # Localiza exclusivamente o botão 'Adicionar envio' para tarefas novas
                submit_button_selectors = [
                    "button:has-text('Adicionar envio')",
                    "input[value*='Adicionar envio']",
                    "a:has-text('Adicionar envio')",
                ]

                action_button = None
                for sel in submit_button_selectors:
                    try:
                        elem = await page.query_selector(sel)
                        if elem and await elem.is_visible():
                            action_button = elem
                            btn_text = await elem.inner_text() if hasattr(elem, "inner_text") else "Adicionar envio"
                            console.print(f"[green]✔ Botão de novo envio localizado: '{btn_text.strip()}'[/green]")
                            break
                    except Exception:
                        continue

                if not action_button:
                    # Verifica se o prazo encerrou ou se a tarefa não aceita envios
                    page_text = await page.inner_text("body")
                    if "não está aceitando envios" in page_text or "tarefa está fechada" in page_text:
                        return False, "O Moodle indica que esta tarefa não está aceitando envios no momento."
                    return False, "Botão 'Adicionar envio' não encontrado (tarefa não aceita novos envios ou já foi entregue)."

                if dry_run:
                    return True, "Modo de simulação concluído com sucesso: botão de envio validado."

                # Clica para abrir a tela de formulário de submissão
                console.print("[cyan]Abrindo formulário de entrega no Moodle...[/cyan]")
                await action_button.click()
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2.0)

                # 2. Upload do Arquivo no Moodle (FilePicker)
                uploaded_ok = False

                # Estratégia A: Input de arquivo direto (comum no Moodle moderno via drag&drop)
                file_inputs = await page.query_selector_all("input[type='file']")
                for fi in file_inputs:
                    try:
                        await fi.set_input_files(str(file_path))
                        console.print(f"  [green]+ Arquivo injetado via seletor direto:[/green] {file_path.name}")
                        uploaded_ok = True
                        await asyncio.sleep(2.5)
                        break
                    except Exception:
                        continue

                # Estratégia B: Modal interativo do FilePicker do Moodle
                if not uploaded_ok:
                    try:
                        console.print("  [dim]Tentando upload via modal do FilePicker...[/dim]")
                        add_icon = await page.query_selector("a[title*='Adicionar'], .fp-btn-add a, a.fp-btn-add")
                        if add_icon:
                            await add_icon.click()
                            await page.wait_for_selector(".moodle-dialogue-base", timeout=10000)

                            # Clica na aba 'Enviar um arquivo' se existir
                            upload_tab = await page.query_selector("span:has-text('Enviar um arquivo'), .fp-repo-name:has-text('Enviar um arquivo')")
                            if upload_tab:
                                await upload_tab.click()

                            # Preenche o input do modal
                            modal_input = await page.wait_for_selector("input[type='file'][name='repo_upload_file']", timeout=5000)
                            if modal_input:
                                await modal_input.set_input_files(str(file_path))
                                upload_btn = await page.query_selector(".fp-upload-btn, button:has-text('Enviar este arquivo')")
                                if upload_btn:
                                    await upload_btn.click()
                                    await page.wait_for_selector(".moodle-dialogue-base", state="hidden", timeout=15000)
                                    uploaded_ok = True
                                    console.print("  [green]+ Arquivo anexado via FilePicker modal com sucesso![/green]")
                    except Exception as fp_err:
                        console.print(f"  [yellow]Nota no FilePicker: {fp_err}[/yellow]")

                # 3. Preenchimento de Texto Online (se disponível e fornecido)
                if text_content:
                    try:
                        online_editor = await page.query_selector("#id_onlinetext_editoreditable, [contenteditable='true']")
                        if online_editor:
                            await online_editor.fill(text_content)
                            console.print("  [green]+ Texto da resolução preenchido no editor online.[/green]")
                    except Exception:
                        pass

                # 4. Salvar Mudanças
                save_btn = await page.query_selector("#id_submitbutton, input[name='submitbutton'], input[value*='Salvar']")
                if not save_btn:
                    return False, "Botão 'Salvar mudanças' não encontrado no formulário."

                console.print("[bold yellow]Confirmando submissão ('Salvar mudanças')...[/bold yellow]")
                await save_btn.click()
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3.0)

                # 5. Validação do Novo Status
                final_text = await page.inner_text("body")
                is_success = (
                    "Enviado para avaliação" in final_text
                    or file_path.name in final_text
                    or "Relatório" in final_text
                )

                if is_success:
                    msg = f"Atividade submetida com sucesso no Moodle UFMG! Arquivo confirmado: {file_path.name}"
                    console.print(f"[bold green]✔ {msg}[/bold green]")
                    return True, msg
                else:
                    return False, "Submissão enviada, mas o Moodle não exibiu a confirmação padrão de avaliação."

            except Exception as e:
                err_msg = f"Falha durante o processo de envio no Moodle: {e}"
                console.print(f"[bold red]{err_msg}[/bold red]")
                return False, err_msg
            finally:
                await browser.close()

    async def submit_quiz(
        self,
        quiz_url: str,
        answers: List[Dict[str, Any]],
        auto_submit: bool = True
    ) -> Tuple[bool, str]:
        """Executa o preenchimento interativo e envio do questionário no Moodle."""
        from src.scraper.moodle_quiz import MoodleQuizAutomator
        automator = MoodleQuizAutomator(auth=self.auth)
        res = await automator.fill_and_submit_quiz(quiz_url=quiz_url, answers=answers, auto_submit=auto_submit)
        if res.get("success"):
            if res.get("already_completed"):
                return True, "Questionário já se encontra finalizado no Moodle."
            if res.get("draft_saved"):
                return True, "Respostas inseridas e salvas como rascunho no Moodle! A tentativa está aberta para conferência."
            grade_info = res.get("grade_info", {})
            grade_str = f" (Nota: {grade_info.get('gradeText')})" if grade_info.get("gradeText") else ""
            msg = f"Questionário preenchido e enviado com sucesso no Moodle!{grade_str}"
            return True, msg
        else:
            return False, res.get("error", "Erro desconhecido ao submeter questionário.")

    async def finalize_quiz(
        self,
        quiz_url: str
    ) -> Tuple[bool, str]:
        """Finaliza e envia definitivamente uma tentativa previamente preenchida no Moodle."""
        from src.scraper.moodle_quiz import MoodleQuizAutomator
        automator = MoodleQuizAutomator(auth=self.auth)
        res = await automator.finalize_submitted_quiz(quiz_url=quiz_url)
        if res.get("success"):
            grade_info = res.get("grade_info", {})
            grade_str = f" (Nota: {grade_info.get('gradeText')})" if grade_info.get("gradeText") else ""
            msg = f"Questionário finalizado e entregue com sucesso no Moodle!{grade_str}"
            return True, msg
        else:
            return False, res.get("error", "Erro ao finalizar questionário no Moodle.")


async def main():
    """CLI para testar o Moodle Submitter."""
    parser = argparse.ArgumentParser(description="Moodle Submitter - UFMG Virtual")
    parser.add_argument("--url", required=True, help="URL da tarefa no Moodle")
    parser.add_argument("--file", required=True, help="Caminho do arquivo a submeter")
    parser.add_argument("--dry-run", action="store_true", help="Apenas simula a navegação sem submeter")
    args = parser.parse_args()

    submitter = MoodleSubmitter()
    success, message = await submitter.submit_assignment(
        assignment_url=args.url,
        file_path=Path(args.file),
        dry_run=args.dry_run
    )

    if success:
        console.print(f"[bold green]Resultado: {message}[/bold green]")
        sys.exit(0)
    else:
        console.print(f"[bold red]Resultado: {message}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
