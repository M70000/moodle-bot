"""Módulo de autenticação para o Moodle / UFMG Virtual via Playwright.

Gerencia login interativo via MinhaUFMG (SSO / IDP), persistência de cookies
e validação de sessões existentes em segundo plano (headless).
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

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()

# Seletores comuns no Moodle e UFMG Virtual que indicam usuário autenticado
AUTHENTICATED_SELECTORS = [
    ".usermenu",
    ".usertext",
    ".userpicture",
    "#action-menu-toggle-1",
    "a[data-title='profile,moodle']",
    "a[href*='login/logout.php']",
    "a[href*='logout']",
    "#menu_minhas_turmas",
    ".card-body",
    ".course-info-container",
]


class MoodleAuth:
    """Gerenciador de autenticação e sessão no Moodle UFMG (virtual.ufmg.br / minhaUFMG)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        cookies_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ):
        self.base_url = (base_url or settings.MOODLE_BASE_URL).rstrip("/")
        self.cookies_path = cookies_path or settings.STORAGE_COOKIES_PATH
        self.timeout_ms = (timeout_seconds or settings.LOGIN_TIMEOUT_SECONDS) * 1000

    @property
    def session_exists(self) -> bool:
        """Verifica se o arquivo de sessão existe no disco e não está vazio."""
        return self.cookies_path.exists() and self.cookies_path.stat().st_size > 0

    async def validate_session(self) -> Tuple[bool, Optional[str]]:
        """Verifica de forma rápida e silenciosa (headless) se a sessão salva ainda é válida.

        Returns:
            Tuple[bool, Optional[str]]: (sucesso, nome_usuario_se_encontrado)
        """
        if not self.session_exists:
            console.print(f"[yellow]Arquivo de sessão não encontrado em: {self.cookies_path}[/yellow]")
            return False, None

        console.print(f"[cyan]Validando sessão existente em {self.base_url}...[/cyan]")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    storage_state=str(self.cookies_path)
                )
                page = await context.new_page()

                # Acessa a página de Minhas Turmas ou raiz do UFMG Virtual
                check_url = f"{self.base_url}/minhasturmas"
                response = await page.goto(check_url, wait_until="domcontentloaded", timeout=20000)

                # Se redirecionou para tela de login do IDP / MinhaUFMG
                current_url = page.url
                if "idp/login.jsp" in current_url or "sistemas.ufmg.br" in current_url:
                    console.print("[red]Sessão expirada: redirecionado para o login do MinhaUFMG.[/red]")
                    return False, None

                # Se permaneceu no domínio do UFMG Virtual e status HTTP OK
                if "virtual.ufmg.br" in current_url:
                    # Tenta capturar nome de usuário ou confirmação visual
                    user_name = None
                    try:
                        name_elem = await page.query_selector(
                            ".usertext, .usermenu .userbutton, #user-menu-toggle, .user-name, [class*='user']"
                        )
                        if name_elem:
                            text = (await name_elem.inner_text()).strip()
                            if text and "login" not in text.lower():
                                user_name = text
                    except Exception:
                        pass

                    console.print(
                        f"[green]✔ Sessão válida e ativa no UFMG Virtual![/green] "
                        f"{f'(Usuário: {user_name})' if user_name else ''}"
                    )
                    return True, user_name

                # Teste alternativo caso haja redirecionamento para algum semestre específico
                for selector in AUTHENTICATED_SELECTORS:
                    try:
                        if await page.is_visible(selector):
                            console.print("[green]✔ Sessão autenticada confirmada por elemento na página![/green]")
                            return True, None
                    except Exception:
                        continue

                console.print("[yellow]Não foi possível confirmar o login na página carregada.[/yellow]")
                return False, None

            except Exception as e:
                console.print(f"[red]Erro ao validar sessão: {e}[/red]")
                return False, None
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def interactive_login(self, headless: Optional[bool] = None) -> bool:
        """Abre o navegador para o usuário realizar login manualmente no MinhaUFMG.

        Navega para https://virtual.ufmg.br/minhasturmas, que redireciona automaticamente
        para https://sistemas.ufmg.br/idp/login.jsp. Após o login, captura a sessão.

        Args:
            headless: Se True, oculta o navegador. Padrão é False para interação.

        Returns:
            bool: True se o login foi concluído com sucesso e os cookies foram salvos.
        """
        is_headless = headless if headless is not None else settings.HEADLESS_LOGIN

        # URL de entrada ideal que engatilha o SSO para o UFMG Virtual
        entry_url = f"{self.base_url}/minhasturmas"

        console.print(
            Panel.fit(
                "[bold cyan]Autenticação UFMG Virtual (MinhaUFMG SSO)[/bold cyan]\n\n"
                f"1. Uma janela do Chromium será aberta e você verá a tela de login do [bold]MinhaUFMG[/bold].\n"
                "2. Digite seu [bold]USUÁRIO[/bold] e [bold]SENHA[/bold] institucional e clique em [bold]Entrar[/bold].\n"
                "3. O sistema será redirecionado para o [bold]UFMG Virtual (Moodle)[/bold].\n"
                "4. Assim que carregar suas disciplinas/turmas, a sessão será salva automaticamente!\n"
                f"5. Tempo limite: [yellow]{self.timeout_ms // 1000} segundos[/yellow].",
                title="[bold yellow]Passo a Passo de Autenticação[/bold yellow]",
                border_style="cyan"
            )
        )

        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=is_headless,
                args=["--start-maximized"]
            )
            try:
                context = await browser.new_context(no_viewport=True)
                page = await context.new_page()

                console.print(f"[blue]Acessando {entry_url}...[/blue]")
                try:
                    await page.goto(entry_url, timeout=30000)
                except Exception as nav_err:
                    console.print(f"[yellow]Nota na navegação inicial: {nav_err}[/yellow]")

                console.print("[bold yellow]Aguardando você realizar o login no navegador...[/bold yellow]")

                start_time = asyncio.get_event_loop().time()
                timeout_time = start_time + (self.timeout_ms / 1000)
                logged_in = False
                detected_user = None

                while asyncio.get_event_loop().time() < timeout_time:
                    # Verifica se o navegador foi fechado pelo usuário
                    if not browser.is_connected():
                        console.print("[bold yellow]Navegador fechado antes da conclusão do login.[/bold yellow]")
                        return False

                    # Varre todas as abas abertas no contexto (caso abra em nova aba)
                    for current_page in list(context.pages):
                        if current_page.is_closed():
                            continue

                        try:
                            url = current_page.url

                            # Condição 1: Usuário voltou para o domínio virtual.ufmg.br e saiu da tela de login
                            is_on_virtual = (
                                "virtual.ufmg.br" in url
                                and "idp/login.jsp" not in url
                                and "login" not in url.lower()
                            )

                            # Condição 2: Usuário está no portal MinhaUFMG autenticado
                            is_on_sistemas_portal = (
                                "sistemas.ufmg.br" in url
                                and "idp/login.jsp" not in url
                                and ("portal" in url or "principal" in url)
                            )

                            if is_on_virtual or is_on_sistemas_portal:
                                # Verifica se a página já carregou conteúdo autenticado
                                for sel in AUTHENTICATED_SELECTORS:
                                    try:
                                        if await current_page.is_visible(sel):
                                            logged_in = True
                                            break
                                    except Exception:
                                        pass

                                # Se está em virtual.ufmg.br (minhasturmas ou ano-semestre), consideramos logado
                                if is_on_virtual and ("minhas" in url or any(c.isdigit() for c in url)):
                                    logged_in = True

                            if logged_in:
                                # Tenta pegar nome do usuário
                                try:
                                    name_elem = await current_page.query_selector(
                                        ".usertext, .usermenu, .user-name, #header-user"
                                    )
                                    if name_elem:
                                        detected_user = (await name_elem.inner_text()).strip()
                                except Exception:
                                    pass
                                break

                        except Exception:
                            # Ignora erros transitórios se a página estiver navegando
                            continue

                    if logged_in:
                        break

                    await asyncio.sleep(1.5)

                if not logged_in:
                    console.print("[bold red]Tempo esgotado para o login manual.[/bold red]")
                    return False

                # Aguarda 2.5s para assegurar persistência de todos os cookies de sessão
                console.print("[cyan]Login detectado! Finalizando gravação dos cookies de sessão...[/cyan]")
                await asyncio.sleep(2.5)

                # Salva o estado completo da sessão (cookies de virtual.ufmg.br e sistemas.ufmg.br)
                await context.storage_state(path=str(self.cookies_path))
                console.print(
                    Panel.fit(
                        f"[bold green]✔ Sessão autenticada com sucesso![/bold green]\n"
                        f"Salva em: [bold]{self.cookies_path}[/bold]\n"
                        f"{f'Usuário: [bold cyan]{detected_user}[/bold cyan]' if detected_user else ''}\n\n"
                        "Nas próximas execuções, o bot rodará 100% em segundo plano (headless)!",
                        title="[bold green]Sucesso[/bold green]",
                        border_style="green"
                    )
                )
                return True

            except Exception as e:
                console.print(f"[bold red]Erro durante o processo de login: {e}[/bold red]")
                return False
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def ensure_authenticated(self, force_login: bool = False) -> bool:
        """Garante que haja uma sessão válida. Se não houver, inicia o fluxo de login.

        Args:
            force_login: Se True, ignora a sessão atual e força novo login interativo.

        Returns:
            bool: True se autenticado com sucesso.
        """
        if not force_login and self.session_exists:
            valid, user = await self.validate_session()
            if valid:
                return True
            console.print("[yellow]Sessão anterior inválida ou expirada. Iniciando novo login...[/yellow]")

        return await self.interactive_login()

    async def get_authenticated_context(
        self, playwright_instance, headless: bool = True
    ) -> Tuple[Browser, BrowserContext]:
        """Inicializa um navegador e contexto já autenticado com a sessão salva.

        Útil para os módulos de Scraper e Scheduler.
        """
        if not self.session_exists:
            raise RuntimeError(
                f"Sessão não encontrada em {self.cookies_path}. Execute o login primeiro."
            )

        browser = await playwright_instance.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(self.cookies_path))
        return browser, context


async def main():
    """CLI para teste e gerenciamento de autenticação do Moodle / UFMG Virtual."""
    parser = argparse.ArgumentParser(
        description="Gerenciador de autenticação UFMG Virtual / MinhaUFMG (Playwright)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Apenas valida a sessão salva atual, sem abrir navegador para login."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Força um novo login interativo mesmo que a sessão atual pareça válida."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Executa o login em modo headless (apenas para testes automatizados)."
    )

    args = parser.parse_args()

    auth = MoodleAuth()

    if args.check:
        valid, user = await auth.validate_session()
        if valid:
            console.print(f"[green]Status: Sessão ativa e pronta para uso! {f'(Usuário: {user})' if user else ''}[/green]")
            sys.exit(0)
        else:
            console.print("[red]Status: Sessão inativa ou inexistente. Execute sem --check para logar.[/red]")
            sys.exit(1)

    if args.force:
        success = await auth.interactive_login(headless=args.headless)
    else:
        success = await auth.ensure_authenticated()

    if success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
