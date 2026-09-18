"""Módulo de autenticação para o Canvas LMS via Playwright e Cookies de Sessão.

Gerencia login interativo no Canvas (com suporte a SSO institucional e 2FA),
login automático via credenciais institucionais, persistência de cookies de sessão
em storage/cookies/canvas_session.json e validação de sessões existentes em segundo plano.
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
from playwright.async_api import async_playwright
from rich.console import Console
from rich.panel import Panel

from config.settings import PROJECT_ROOT, settings

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console()

# Seletores padrão do Canvas LMS que indicam que o usuário está autenticado
AUTHENTICATED_CANVAS_SELECTORS = [
    "#dashboard",
    "#dashboard_header_container",
    ".ic-Dashboard-header__title",
    "#global_nav_courses_link",
    "#global_nav_calendar_link",
    "a[href*='/courses']",
    "a[href*='/logout']",
    "#primaryNavToggle",
    ".menu-item__text",
    "#header",
]


class CanvasAuth:
    """Gerenciador de autenticação e sessão no Canvas LMS."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        cookies_path: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
    ):
        raw_url = base_url or getattr(settings, "CANVAS_BASE_URL", "https://pucminas.instructure.com")
        self.base_url = raw_url.rstrip("/")
        self.cookies_path = (
            cookies_path
            or getattr(settings, "CANVAS_COOKIES_PATH", None)
            or (getattr(settings, "PROJECT_ROOT", None) or PROJECT_ROOT) / "storage" / "cookies" / "canvas_session.json"
        )
        self.timeout_ms = (timeout_seconds or getattr(settings, "LOGIN_TIMEOUT_SECONDS", 300)) * 1000

    @property
    def session_exists(self) -> bool:
        """Verifica se o arquivo de sessão do Canvas existe e contém dados."""
        return self.cookies_path.exists() and self.cookies_path.stat().st_size > 10

    def get_cookies_dict(self) -> Dict[str, str]:
        """Lê o arquivo de estado de armazenamento do Playwright e extrai um dicionário de cookies."""
        if not self.session_exists:
            return {}
        try:
            data = json.loads(self.cookies_path.read_text(encoding="utf-8"))
            cookies_list = data.get("cookies", [])
            cookies_dict: Dict[str, str] = {}
            for c in cookies_list:
                name = c.get("name")
                value = c.get("value")
                if name and value:
                    cookies_dict[name] = value
            return cookies_dict
        except Exception as e:
            console.print(f"[yellow]Aviso ao ler cookies do Canvas: {e}[/yellow]")
            return {}

    def get_csrf_token(self) -> Optional[str]:
        """Extrai o token CSRF dos cookies do Canvas (_csrf_token)."""
        cookies = self.get_cookies_dict()
        raw_csrf = cookies.get("_csrf_token")
        if raw_csrf:
            try:
                return urllib.parse.unquote(raw_csrf)
            except Exception:
                return raw_csrf
        return None

    async def validate_session(self) -> Tuple[bool, Optional[str]]:
        """Valida se a sessão salva ainda é aceita pelo Canvas LMS.

        Realiza uma consulta rápida à rota /api/v1/users/self utilizando os cookies de sessão.

        Returns:
            Tuple[bool, Optional[str]]: (sucesso, nome_do_aluno_se_autenticado)
        """
        if not self.session_exists:
            return False, None

        cookies = self.get_cookies_dict()
        if not cookies:
            return False, None

        url = f"{self.base_url}/api/v1/users/self"
        headers = {
            "Accept": "application/json",
            "User-Agent": "LumiBot-CanvasAuth/2.0",
        }
        csrf = self.get_csrf_token()
        if csrf:
            headers["X-CSRF-Token"] = csrf

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                res = await client.get(url, headers=headers, cookies=cookies)
                if res.status_code == 200:
                    data = res.json()
                    user_name = data.get("name") or data.get("short_name") or data.get("login_id") or "Aluno Autenticado"
                    return True, user_name
                if res.status_code in (401, 403, 302):
                    return False, None
        except Exception:
            pass

        return False, None

    async def interactive_login(self, headless: Optional[bool] = None) -> bool:
        """Abre o navegador para o usuário realizar login manualmente no Canvas LMS.

        Permite autenticação via SSO institucional, Google, Microsoft ou login padrão,
        com suporte total a duplo fator (2FA). Ao concluir, salva os cookies.

        Args:
            headless: Se True, oculta a interface gráfica (padrão é False para permitir interação).

        Returns:
            bool: True se o login foi detectado e salvo com sucesso.
        """
        is_headless = headless if headless is not None else False
        entry_url = self.base_url

        console.print(
            Panel.fit(
                "[bold cyan]Autenticação Canvas LMS (Login Interativo)[/bold cyan]\n\n"
                f"1. Uma janela do Chromium será aberta em [bold]{entry_url}[/bold].\n"
                "2. Digite suas credenciais institucionais e conclua o 2FA/SSO se exigido.\n"
                "3. Quando o painel principal do Canvas carregar, a sessão será salva automaticamente!\n"
                f"4. Tempo limite: [yellow]{self.timeout_ms // 1000} segundos[/yellow].",
                title="[bold yellow]Passo a Passo de Autenticação[/bold yellow]",
                border_style="cyan",
            )
        )

        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=is_headless,
                args=["--start-maximized"],
            )
            try:
                context = await browser.new_context(no_viewport=True)
                page = await context.new_page()

                console.print(f"[blue]Acessando {entry_url}...[/blue]")
                try:
                    await page.goto(entry_url, timeout=30000)
                except Exception as nav_err:
                    console.print(f"[yellow]Nota na navegação inicial: {nav_err}[/yellow]")

                console.print("[bold yellow]Aguardando você concluir o login no navegador...[/bold yellow]")

                start_time = asyncio.get_event_loop().time()
                timeout_time = start_time + (self.timeout_ms / 1000)
                logged_in = False
                detected_user = None

                while asyncio.get_event_loop().time() < timeout_time:
                    if not browser.is_connected():
                        console.print("[bold yellow]Navegador fechado antes da conclusão do login.[/bold yellow]")
                        return False

                    for current_page in list(context.pages):
                        if current_page.is_closed():
                            continue

                        try:
                            url = current_page.url.lower()

                            # Verifica se o usuário saiu da tela de login e está dentro do Canvas
                            is_in_canvas = any(domain_part in url for domain_part in ["instructure.com", "canvas"])
                            not_in_login = not any(k in url for k in ["/login", "login.jsp", "sso", "saml"])

                            if is_in_canvas and not_in_login:
                                for sel in AUTHENTICATED_CANVAS_SELECTORS:
                                    try:
                                        if await current_page.is_visible(sel):
                                            logged_in = True
                                            break
                                    except Exception:
                                        pass

                            if not logged_in and ("dashboard" in url or "courses" in url):
                                logged_in = True

                            if logged_in:
                                try:
                                    name_elem = await current_page.query_selector(
                                        ".ic-avatar + span, .user_name, #global_nav_profile_link, .avatar-wrapper"
                                    )
                                    if name_elem:
                                        detected_user = (await name_elem.inner_text()).strip()
                                except Exception:
                                    pass
                                break

                        except Exception:
                            continue

                    if logged_in:
                        break

                    await asyncio.sleep(1.5)

                if not logged_in:
                    console.print("[bold red]Tempo esgotado para o login manual no Canvas.[/bold red]")
                    return False

                console.print("[cyan]Login detectado! Finalizando gravação dos cookies de sessão do Canvas...[/cyan]")
                await asyncio.sleep(2.5)

                await context.storage_state(path=str(self.cookies_path))
                console.print(
                    Panel.fit(
                        f"[bold green]✔ Sessão do Canvas autenticada com sucesso![/bold green]\n"
                        f"Salva em: [bold]{self.cookies_path}[/bold]\n"
                        f"{f'Usuário: [bold cyan]{detected_user}[/bold cyan]' if detected_user else ''}\n\n"
                        "O bot utilizará essa sessão para todas as chamadas à API REST v1 do Canvas!",
                        title="[bold green]Sucesso[/bold green]",
                        border_style="green",
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
        """Realiza login automático e silencioso via credenciais no Canvas LMS usando Playwright.

        Navega até a tela de login do Canvas ou SSO institucional, preenche os campos,
        valida a resposta e persiste os cookies de sessão de forma totalmente autônoma.

        Args:
            username: Usuário/e-mail institucional (se omitido, lê CANVAS_USERNAME).
            password: Senha institucional (se omitido, lê CANVAS_PASSWORD).
            headless: Se True, roda sem interface gráfica visível.
            timeout_seconds: Tempo limite da operação em segundos.

        Returns:
            Tuple[bool, Optional[str]]: (sucesso, nome_usuario_ou_erro)
        """
        user = (username or getattr(settings, "CANVAS_USERNAME", "") or "").strip()
        pwd = (password or getattr(settings, "CANVAS_PASSWORD", "") or "").strip()

        if not user or not pwd:
            msg = "Credenciais do Canvas (CANVAS_USERNAME / CANVAS_PASSWORD) não foram configuradas."
            console.print(f"[bold red]❌ {msg}[/bold red]")
            return False, msg

        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        timeout_ms = (timeout_seconds or 45) * 1000

        console.print(
            f"[bold cyan]🔐 Iniciando autenticação automática no Canvas para o usuário '[bold]{user}[/bold]' (headless={headless})...[/bold cyan]"
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(no_viewport=True)
                page = await context.new_page()

                # 1. Navegação inicial: tenta a base_url para seguir eventuais redirecionamentos de SSO
                try:
                    await page.goto(self.base_url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as nav_err:
                    console.print(f"[yellow]Aviso na navegação inicial: {nav_err}[/yellow]")

                # Se já estiver autenticado e redirecionou para o painel
                current_url = page.url.lower()
                if "login_success" in current_url or (
                    "/login" not in current_url and ("dashboard" in current_url or "courses" in current_url)
                ):
                    for sel in AUTHENTICATED_CANVAS_SELECTORS:
                        try:
                            if await page.is_visible(sel):
                                await context.storage_state(path=str(self.cookies_path))
                                return True, user
                        except Exception:
                            pass

                # 2. Verifica se caiu na página de descoberta (ex: iad.login.instructure.com/discovery/...)
                if "discovery" in current_url:
                    console.print("[cyan]Detectada página de descoberta de SSO institucional no Canvas...[/cyan]")
                    try:
                        await page.wait_for_selector(
                            "a[href*='/login/microsoft/'], a[href*='/login/saml/'], a[href*='/login/sso/'], a:has-text('Aluno'), a:has-text('Professor'), button",
                            timeout=10000,
                        )
                    except Exception:
                        pass

                    # Procura link de login de aluno/professor ou Microsoft
                    sso_link = await page.query_selector(
                        "a[href*='/login/microsoft/'], a[href*='/login/saml/'], a[href*='/login/sso/'], a:has-text('Aluno'), a:has-text('Professor'), a:has-text('Entrar')"
                    )
                    if sso_link:
                        try:
                            sso_href = await sso_link.get_attribute("href")
                            if sso_href and sso_href.startswith("http"):
                                await page.goto(sso_href, wait_until="domcontentloaded", timeout=15000)
                            else:
                                await sso_link.click()
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                            current_url = page.url.lower()
                        except Exception as sso_nav_err:
                            console.print(f"[yellow]Aviso ao redirecionar para o SSO institucional: {sso_nav_err}[/yellow]")

                # 3. Tratamento de SSO Microsoft Online (comum em universidades como a PUC Minas)
                if "login.microsoftonline.com" in current_url:
                    console.print("[cyan]Processando autenticação via SSO Institucional Microsoft...[/cyan]")
                    try:
                        ms_user_sel = "input[name='loginfmt'], #i0116, input[type='email']"
                        await page.wait_for_selector(ms_user_sel, timeout=15000)
                        await page.fill(ms_user_sel, user)
                        await page.click("input[type='submit'], #idSIButton9")
                        await asyncio.sleep(1.5)

                        # Verifica erro de usuário no Microsoft
                        ms_user_err = await page.query_selector("#usernameError, #i0116Error, .alert-error")
                        if ms_user_err:
                            err_txt = (await ms_user_err.inner_text()).strip()
                            if err_txt:
                                return False, f"Usuário rejeitado no portal institucional: {err_txt}"

                        # Preenchimento de senha no Microsoft
                        ms_pwd_sel = "input[name='passwd'], #i0118, input[type='password']"
                        await page.wait_for_selector(ms_pwd_sel, timeout=15000)
                        await page.fill(ms_pwd_sel, pwd)
                        await page.click("input[type='submit'], #idSIButton9")
                        await asyncio.sleep(2.0)

                        # Verifica erro de senha no Microsoft
                        ms_pwd_err = await page.query_selector("#passwordError, #i0118Error, .alert-error")
                        if ms_pwd_err:
                            err_txt = (await ms_pwd_err.inner_text()).strip()
                            if err_txt:
                                return False, f"Senha rejeitada no portal institucional: {err_txt}"

                        # Verifica se 2FA (MFA) foi exigido
                        mfa_elem = await page.query_selector(
                            "#idDiv_SAOTCS_Title, #idRichContext_DisplaySign, #idA_SAASTO_Resend, "
                            ".mfa-title, div[data-view-id*='TwoFactor'], #idDiv_SAOTCC_Title"
                        )
                        if mfa_elem:
                            return False, (
                                "Autenticação em Dois Fatores (2FA / Microsoft Authenticator) exigida pelo portal institucional. "
                                "Como o login automático por credenciais é silencioso, utilize a opção 'Iniciar Login no Canvas (Navegador)' "
                                "para concluir o 2FA interativamente ou utilize um Token de API do Canvas."
                            )

                        # Botão 'Manter conectado?' (Stay signed in)
                        stay_in_btn = await page.query_selector("#idSIButton9, input[type='submit']")
                        if stay_in_btn:
                            try:
                                await stay_in_btn.click()
                            except Exception:
                                pass
                    except Exception as ms_err:
                        console.print(f"[yellow]Nota no fluxo Microsoft SSO: {ms_err}[/yellow]")

                # 4. Caso ainda esteja em tela de login não-SSO ou local Canvas
                current_url = page.url.lower()
                is_canvas_login = "login" in current_url or await page.query_selector("#pseudonym_session_unique_id") is not None

                if not is_canvas_login and not any(k in current_url for k in ["instructure.com", "canvas"]):
                    # Fallback para a rota direta /login/canvas
                    try:
                        login_url = f"{self.base_url}/login/canvas"
                        await page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    except Exception:
                        pass

                # Preenche campos padrão do Canvas LMS caso estejam na tela
                user_field = await page.query_selector("#pseudonym_session_unique_id, input[name='pseudonym_session[unique_id]']")
                if user_field:
                    await user_field.fill(user)
                    pwd_field = await page.query_selector("#pseudonym_session_password, input[name='pseudonym_session[password]']")
                    if pwd_field:
                        await pwd_field.fill(pwd)
                        submit_btn = await page.query_selector("input[type='submit'], button[type='submit'], .Button--login")
                        if submit_btn:
                            await submit_btn.click()
                        else:
                            await page.keyboard.press("Enter")

                # 5. Monitora resultado da autenticação e redirecionamento pós-login
                start_time = asyncio.get_event_loop().time()
                timeout_time = start_time + (timeout_ms / 1000)
                logged_in = False
                detected_user = None

                error_selectors = [
                    ".ic-flash-error",
                    ".ic-Flash-error",
                    ".ic-flash-static",
                    ".flash-error",
                    ".error_text",
                    "#flash_message_holder",
                    "#usernameError",
                    "#passwordError",
                    "#i0116Error",
                    "#i0118Error",
                    ".alert-error",
                ]

                while asyncio.get_event_loop().time() < timeout_time:
                    cur_url = page.url.lower()

                    # Verifica imediatamente mensagens de erro exibidas na página
                    for err_sel in error_selectors:
                        try:
                            error_elem = await page.query_selector(err_sel)
                            if error_elem and await error_elem.is_visible():
                                error_text = (await error_elem.inner_text()).strip()
                                if error_text and len(error_text) > 3:
                                    console.print(f"[bold red]❌ Falha na autenticação do Canvas: {error_text}[/bold red]")
                                    return False, f"Credenciais rejeitadas pelo Canvas: {error_text}"
                        except Exception:
                            pass

                    # Verifica se o login foi concluído com sucesso
                    # login_success=1 é o parâmetro padrão retornado pelo Canvas ao autenticar
                    is_login_path = any(p in urllib.parse.urlparse(cur_url).path for p in ["/login", "/discovery", "/sso"])
                    if "login_success" in cur_url or not is_login_path:
                        for sel in AUTHENTICATED_CANVAS_SELECTORS:
                            try:
                                if await page.is_visible(sel):
                                    logged_in = True
                                    break
                            except Exception:
                                pass

                        if not logged_in and ("dashboard" in cur_url or "courses" in cur_url):
                            logged_in = True

                        if logged_in:
                            try:
                                name_elem = await page.query_selector(
                                    ".ic-avatar + span, .user_name, #global_nav_profile_link, .avatar-wrapper"
                                )
                                if name_elem:
                                    detected_user = (await name_elem.inner_text()).strip()
                            except Exception:
                                pass
                            break

                    await asyncio.sleep(1.0)

                if not logged_in:
                    return False, "Tempo esgotado para o login automático no Canvas LMS. Verifique se o portal institucional exige 2FA ou utilize Sessão por Cookies / Token de API."

                await asyncio.sleep(1.5)
                await context.storage_state(path=str(self.cookies_path))
                console.print(f"[bold green]✔ Sessão do Canvas gravada com sucesso para {detected_user or user}![/bold green]")
                return True, detected_user or user

            except Exception as e:
                console.print(f"[bold red]Erro durante login por credenciais no Canvas: {e}[/bold red]")
                return False, str(e)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass


def main():
    """Ponto de entrada de linha de comando para login interativo do Canvas LMS."""
    parser = argparse.ArgumentParser(description="Autenticação do Canvas LMS para o LumiBot.")
    parser.add_argument(
        "--credentials",
        action="store_true",
        help="Realiza o login silencioso via usuário e senha (modo automático).",
    )
    args = parser.parse_args()

    auth = CanvasAuth()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if args.credentials:
            success, msg = loop.run_until_complete(auth.login_with_credentials())
            if not success:
                sys.exit(1)
        else:
            success = loop.run_until_complete(auth.interactive_login())
            if not success:
                sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
