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

    async def heartbeat_session(self, save_refreshed: bool = True) -> Tuple[bool, Optional[str]]:
        """Executa um heartbeat / keep-alive leve ao Moodle para renovar a sessão no servidor.

        Utiliza o request context do Playwright (sem lançar Chromium completo),
        garantindo execução em milissegundos com baixíssimo consumo de memória e CPU.
        Se o Moodle responder com Set-Cookie (renovação de sessão), persiste o estado atualizado.

        Returns:
            Tuple[bool, Optional[str]]: (ativa, mensagem_ou_usuario)
        """
        if not self.session_exists:
            return False, "Arquivo de sessão não encontrado"

        async with async_playwright() as p:
            try:
                request_context = await p.request.new_context(
                    storage_state=str(self.cookies_path),
                    timeout=20000,
                    extra_http_headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    }
                )
                check_url = f"{self.base_url}/minhasturmas"
                response = await request_context.get(check_url, max_redirects=10)
                final_url = response.url

                # Se redirecionou para IDP do MinhaUFMG, a sessão expirou
                if "idp/login.jsp" in final_url or "sistemas.ufmg.br" in final_url:
                    return False, "Sessão expirada (redirecionado para MinhaUFMG IDP)"

                # Se respondeu com sucesso permanecendo no domínio do Moodle
                if response.ok and "virtual.ufmg.br" in final_url:
                    if save_refreshed:
                        try:
                            await request_context.storage_state(path=str(self.cookies_path))
                        except Exception:
                            pass
                    return True, "Sessão ativa e renovada no servidor"

                return False, f"Resposta inesperada do Moodle (HTTP {response.status} em {final_url})"
            except Exception as e:
                # Fallback em caso de erro na camada HTTP direta
                try:
                    val_ok, val_user = await self.validate_session()
                    if val_ok:
                        return True, f"Sessão validada via navegador ({val_user or 'Usuário ativo'})"
                    return False, f"Falha na validação de sessão: {e}"
                except Exception:
                    return False, str(e)

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

    async def login_with_credentials(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        headless: bool = True,
        timeout_seconds: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Realiza login automático e silencioso via credenciais (usuário e senha do MinhaUFMG) usando Playwright.

        Navega até o portal SSO, preenche os campos do formulário IDP, valida resposta
        e persiste os cookies de sessão de forma totalmente autônoma.

        Args:
            username: Usuário do MinhaUFMG (se omitido, usa settings.MOODLE_USERNAME).
            password: Senha do MinhaUFMG (se omitido, usa settings.MOODLE_PASSWORD).
            headless: Se True, executa sem interface gráfica visível.
            timeout_seconds: Tempo limite da operação em segundos.

        Returns:
            Tuple[bool, Optional[str]]: (sucesso, nome_usuario_ou_mensagem_de_erro)
        """
        user = (username or getattr(settings, "MOODLE_USERNAME", "") or "").strip()
        pwd = (password or getattr(settings, "MOODLE_PASSWORD", "") or "").strip()

        if not user or not pwd:
            msg = "Credenciais institucionais (MOODLE_USERNAME / MOODLE_PASSWORD) não foram configuradas."
            console.print(f"[bold red]❌ {msg}[/bold red]")
            return False, msg

        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        timeout_ms = (timeout_seconds or 45) * 1000
        entry_url = f"{self.base_url}/minhasturmas"

        console.print(
            f"[bold cyan]🔐 Iniciando autenticação automática com credenciais para o usuário '[bold]{user}[/bold]' (headless={headless})...[/bold cyan]"
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                context = await browser.new_context(no_viewport=True)
                page = await context.new_page()

                # Navega até a URL de entrada
                try:
                    await page.goto(entry_url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as nav_err:
                    console.print(f"[yellow]Aviso na navegação inicial: {nav_err}[/yellow]")

                # Se porventura já estiver no domínio do UFMG Virtual e autenticado
                current_url = page.url
                if "virtual.ufmg.br" in current_url and "idp/login.jsp" not in current_url and "login" not in current_url.lower():
                    await context.storage_state(path=str(self.cookies_path))
                    return True, user

                # Aguarda os campos de autenticação do IDP MinhaUFMG
                try:
                    await page.wait_for_selector("#j_username, input[name='j_username']", timeout=20000)
                except Exception:
                    return False, f"Página de login do MinhaUFMG não carregou a tempo (URL atual: {page.url})"

                # Preenche usuário e senha no formulário oficial do MinhaUFMG
                await page.fill("#j_username, input[name='j_username']", user)
                await page.fill("#j_password, input[name='j_password']", pwd)

                # Verifica se há captcha já na página inicial
                captcha_elem = await page.query_selector("#box_captcha_image, input[name='cAnswer']")
                if captcha_elem:
                    return False, "Captcha de segurança exigido pelo MinhaUFMG. Realize o login pelo modo interativo (cookies)."

                # Submete o formulário
                submit_btn = await page.query_selector("#submit, input[name='submit'], input[type='submit']")
                if submit_btn:
                    await submit_btn.click()
                else:
                    await page.keyboard.press("Enter")

                # Aguarda processamento e redirecionamento pós-login
                start_time = asyncio.get_event_loop().time()
                timeout_time = start_time + (timeout_ms / 1000)
                detected_user = None
                logged_in = False

                while asyncio.get_event_loop().time() < timeout_time:
                    cur_url = page.url

                    # Verifica se o IDP exibiu aviso de erro de credenciais
                    warning_elem = await page.query_selector("#box_warning")
                    if warning_elem:
                        warning_text = (await warning_elem.inner_text()).strip()
                        if warning_text and any(w in warning_text.lower() for w in ["inválid", "incorret", "erro", "falha"]):
                            console.print(f"[bold red]❌ MinhaUFMG rejeitou as credenciais: {warning_text}[/bold red]")
                            return False, f"Credenciais rejeitadas pelo MinhaUFMG: {warning_text}"

                    # Verifica se apareceu captcha de contingência
                    captcha_elem = await page.query_selector("#box_captcha_image, input[name='cAnswer']")
                    if captcha_elem:
                        return False, "MinhaUFMG solicitou verificação por Captcha. Utilize o login manual via cookies."

                    # Verifica se foi redirecionado com sucesso para o UFMG Virtual
                    if "virtual.ufmg.br" in cur_url and "idp/login.jsp" not in cur_url and "login" not in cur_url.lower():
                        for sel in AUTHENTICATED_SELECTORS:
                            try:
                                if await page.is_visible(sel):
                                    logged_in = True
                                    break
                            except Exception:
                                pass

                        if "minhas" in cur_url or any(c.isdigit() for c in cur_url):
                            logged_in = True

                        if logged_in:
                            try:
                                name_elem = await page.query_selector(
                                    ".usertext, .usermenu, .user-name, #header-user"
                                )
                                if name_elem:
                                    detected_user = (await name_elem.inner_text()).strip()
                            except Exception:
                                pass
                            break

                    await asyncio.sleep(1.0)

                if not logged_in:
                    return False, f"Tempo limite esgotado sem confirmação de login no Moodle (URL final: {page.url})"

                # Aguarda 2s para assegurar persistência dos cookies de sessão
                await asyncio.sleep(2.0)
                await context.storage_state(path=str(self.cookies_path))
                console.print(
                    Panel.fit(
                        f"[bold green]✔ Autenticação automática por credenciais concluída com sucesso![/bold green]\n"
                        f"Salva em: [bold]{self.cookies_path}[/bold]\n"
                        f"Usuário: [bold cyan]{detected_user or user}[/bold cyan]",
                        title="[bold green]Login Automático[/bold green]",
                        border_style="green"
                    )
                )
                return True, detected_user or user

            except Exception as e:
                console.print(f"[bold red]Erro durante login automático com credenciais: {e}[/bold red]")
                return False, str(e)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def ensure_authenticated(self, force_login: bool = False) -> bool:
        """Garante que haja uma sessão válida. Se não houver, autentica conforme o AUTH_MODE configurado.

        - Se AUTH_MODE == 'credentials': tenta auto-login via credenciais em background.
        - Se AUTH_MODE == 'cookies':
          - Se for primeiro login (não há arquivo de sessão): abre o navegador interativo.
          - Se a sessão anterior existia mas expirou: NÃO abre janela automaticamente (a menos que force_login=True).

        Args:
            force_login: Se True, força novo login mesmo que sessão exista ou tenha expirado.

        Returns:
            bool: True se autenticado com sucesso.
        """
        if not force_login and self.session_exists:
            valid, user = await self.validate_session()
            if valid:
                return True
            console.print("[yellow]Sessão anterior inválida ou expirada.[/yellow]")

        auth_mode = getattr(settings, "AUTH_MODE", "cookies").lower()
        if auth_mode == "credentials":
            console.print("[cyan]Modo Credenciais ativo: executando login automático silencioso...[/cyan]")
            ok, user_or_err = await self.login_with_credentials(headless=True)
            if ok:
                return True
            console.print(f"[yellow]Aviso: Falha no login automático por credenciais: {user_or_err}[/yellow]")

        # No modo cookies:
        # Abre o navegador automaticamente apenas no PRIMEIRO LOGIN (arquivo de cookies não existe)
        # ou se explicitamente forçado (ex: clique no botão do Discord / comando CLI).
        if not self.session_exists or force_login:
            console.print("[cyan]Primeiro login (ou forçado): Abrindo navegador interativo...[/cyan]")
            return await self.interactive_login()

        console.print(
            "[yellow]Modo cookies: Sessão expirada. O navegador não será aberto automaticamente. "
            "Aguardando autorização via botão do Discord.[/yellow]"
        )
        return False

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
        "--ping",
        "--heartbeat",
        dest="ping",
        action="store_true",
        help="Executa o heartbeat leve de keep-alive para renovar a sessão no servidor."
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
    parser.add_argument(
        "--mode",
        choices=["cookies", "credentials"],
        default=None,
        help="Modo de autenticação a utilizar ('cookies' ou 'credentials')."
    )
    parser.add_argument(
        "--user",
        "--username",
        dest="username",
        default=None,
        help="Usuário institucional do MinhaUFMG (para modo credenciais)."
    )
    parser.add_argument(
        "--password",
        dest="password",
        default=None,
        help="Senha institucional do MinhaUFMG (para modo credenciais)."
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Executa o login automático por credenciais imediatamente."
    )

    args = parser.parse_args()

    auth = MoodleAuth()

    if args.ping:
        active, msg = await auth.heartbeat_session()
        if active:
            console.print(f"[green]✔ Heartbeat OK: {msg}[/green]")
            sys.exit(0)
        else:
            console.print(f"[red]❌ Heartbeat falhou: {msg}[/red]")
            sys.exit(1)

    if args.check:
        valid, user = await auth.validate_session()
        if valid:
            console.print(f"[green]Status: Sessão ativa e pronta para uso! {f'(Usuário: {user})' if user else ''}[/green]")
            sys.exit(0)
        else:
            console.print("[red]Status: Sessão inativa ou inexistente. Execute sem --check para logar.[/red]")
            sys.exit(1)

    # Modo credenciais forçado via flag
    if args.auto or args.mode == "credentials" or (args.username and args.password):
        ok, user_or_err = await auth.login_with_credentials(
            username=args.username,
            password=args.password,
            headless=args.headless
        )
        if ok:
            console.print(f"[bold green]✔ Login por credenciais bem-sucedido! Usuário: {user_or_err}[/bold green]")
            sys.exit(0)
        else:
            console.print(f"[bold red]❌ Falha no login por credenciais: {user_or_err}[/bold red]")
            sys.exit(1)

    if args.force:
        success = await auth.interactive_login(headless=args.headless)
    else:
        success = await auth.ensure_authenticated(force_login=False)

    if success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
