@echo off
chcp 65001 >nul
title LumiBot - Assistente de Configuração
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem 1. Detecção do Interpretador Python
rem ------------------------------------------------------------------
set "PYTHON_EXEC=python"
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXEC=.venv\Scripts\python.exe"
)

rem ------------------------------------------------------------------
rem 2. Inicialização do Arquivo .env
rem ------------------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
    ) else (
        type nul > ".env"
    )
)

:MENU_PRINCIPAL
cls
echo ==================================================================
echo     __    __  ____  ________  ____  ____  ______
echo    / /   / / / /  ^|/  /  _/  / __ )/ __ \/_  __/
echo   / /   / / / / /^|_/ // /   / __  / / / / / /   
echo  / /___/ /_/ / /  / // /   / /_/ / /_/ / / /    
echo /_____/\____/_/  /_/___/  /_____/\____/ /_/     
echo.
echo           💡 LumiBot • Seu Copiloto Acadêmico
echo ==================================================================
echo.
echo Escolha o módulo de configuração desejado:
echo.
echo  [1] Configurar Discord ^& IA (Token do Bot, Canais e Chave Gemini/DeepSeek)
echo  [2] Configurar Plataformas Acadêmicas (Canvas LMS e/ou Moodle)
echo  [3] Configuração Rápida Guiada (Passo a Passo Completo)
echo  [4] Abrir Painel Visual Web (Interface do Navegador)
echo  [5] Sair
echo.
set "OPCAO="
set /p "OPCAO=Digite a opção desejada (1-5): "

if "%OPCAO%"=="" goto MENU_PRINCIPAL
if "%OPCAO:~0,1%"=="1" goto CONFIG_DISCORD_IA
if "%OPCAO:~0,1%"=="2" goto CONFIG_LMS
if "%OPCAO:~0,1%"=="3" goto CONFIG_GUIADA
if "%OPCAO:~0,1%"=="4" goto ABRIR_WEB
if "%OPCAO:~0,1%"=="5" goto SAIR

echo.
echo [!] Opção inválida! Pressione qualquer tecla para tentar novamente.
pause >nul
goto MENU_PRINCIPAL


rem ------------------------------------------------------------------
rem Módulo 1: Discord & Provedores de IA
rem ------------------------------------------------------------------
:CONFIG_DISCORD_IA
cls
echo ==================================================================
echo         [1/2] Configuração do Discord ^& Inteligência Artificial
echo ==================================================================
echo.
echo Configure abaixo as credenciais para conexão do LumiBot.
echo (Dica: Pressione ENTER sem digitar nada para MANTER o valor atual)
echo.

%PYTHON_EXEC% -c "from pathlib import Path; p = Path('.env'); lines = p.read_text(encoding='utf-8', errors='ignore').splitlines() if p.exists() else []; cfg = dict(l.strip().split('=', 1) for l in lines if '=' in l and not l.strip().startswith('#')); tok = cfg.get('DISCORD_BOT_TOKEN', ''); tok_disp = (tok[:8] + '...' + tok[-5:]) if len(tok) > 13 else (tok if tok else 'Não configurado'); ch = cfg.get('DISCORD_CHANNEL_ID', '0'); gem = cfg.get('GEMINI_API_KEY', ''); gem_disp = (gem[:6] + '...' + gem[-4:]) if len(gem) > 10 else (gem if gem else 'Não configurado'); prov = cfg.get('AI_PROVIDER', 'gemini'); print(f'• Token do Bot do Discord (Atual: {tok_disp})'); print(f'• Canal ID do Discord (Atual: {ch})'); print(f'• Chave Google Gemini (Atual: {gem_disp})'); print(f'• Provedor de IA Atual: {prov}')"

echo.
set "NEW_DISCORD_TOKEN="
set /p "NEW_DISCORD_TOKEN=Novo Token do Bot [Enter para manter]: "
if not "%NEW_DISCORD_TOKEN%"=="" (
    call :SET_ENV_VAR "DISCORD_BOT_TOKEN" "%NEW_DISCORD_TOKEN%"
)

set "NEW_DISCORD_CH="
set /p "NEW_DISCORD_CH=Novo Canal ID do Discord [Enter para manter]: "
if not "%NEW_DISCORD_CH%"=="" (
    call :SET_ENV_VAR "DISCORD_CHANNEL_ID" "%NEW_DISCORD_CH%"
)

set "NEW_GEMINI_KEY="
set /p "NEW_GEMINI_KEY=Nova Chave Google Gemini [Enter para manter]: "
if not "%NEW_GEMINI_KEY%"=="" (
    call :SET_ENV_VAR "GEMINI_API_KEY" "%NEW_GEMINI_KEY%"
)

echo.
echo Provedor Primário de IA:
echo   [1] Google Gemini (Recomendado - Multimodal)
echo   [2] DeepSeek (Flash / Raciocínio - BYOK)
echo   [3] Anthropic Claude (Haiku / Sonnet)
set "PROV_CHOICE="
set /p "PROV_CHOICE=Escolha o provedor (1-3) [Enter para manter]: "

if "%PROV_CHOICE:~0,1%"=="1" (
    call :SET_ENV_VAR "AI_PROVIDER" "gemini"
) else if "%PROV_CHOICE:~0,1%"=="2" (
    call :SET_ENV_VAR "AI_PROVIDER" "deepseek"
    set "NEW_DS_KEY="
    set /p "NEW_DS_KEY=Chave de API DeepSeek (api.deepseek.com) [Enter para manter]: "
    if not "%NEW_DS_KEY%"=="" (
        call :SET_ENV_VAR "DEEPSEEK_API_KEY" "%NEW_DS_KEY%"
    )
) else if "%PROV_CHOICE:~0,1%"=="3" (
    call :SET_ENV_VAR "AI_PROVIDER" "claude"
    set "NEW_CLAUDE_KEY="
    set /p "NEW_CLAUDE_KEY=Chave de API Anthropic Claude [Enter para manter]: "
    if not "%NEW_CLAUDE_KEY%"=="" (
        call :SET_ENV_VAR "ANTHROPIC_API_KEY" "%NEW_CLAUDE_KEY%"
    )
)

echo.
echo [OK] Configurações de Discord e IA salvas com sucesso!
timeout /t 2 >nul

if "%MODO_GUIADO%"=="1" goto CONFIG_LMS_PROMPT
goto MENU_PRINCIPAL


rem ------------------------------------------------------------------
rem Módulo 2: Plataformas Acadêmicas (Multi-LMS)
rem ------------------------------------------------------------------
:CONFIG_LMS
set "MODO_GUIADO=0"
:CONFIG_LMS_PROMPT
cls
echo ==================================================================
echo             [2/2] Plataformas Acadêmicas (Multi-LMS)
echo ==================================================================
echo.
echo O LumiBot possui arquitetura Multi-LMS universal e pode sincronizar
echo tanto com o Canvas LMS quanto com o Moodle.
echo.
echo Selecione a sua faculdade / plataforma:
echo  [1] Canvas LMS (PUC-Rio / Instructure)
echo  [2] Moodle (UFMG Virtual / Outras Universidades)
echo  [3] Ambas as Plataformas Simultaneamente (Multi-LMS)
echo  [4] Voltar ao Menu Principal
echo.
set "LMS_CHOICE="
set /p "LMS_CHOICE=Digite a opção (1-4): "

if "%LMS_CHOICE:~0,1%"=="1" goto SETUP_CANVAS
if "%LMS_CHOICE:~0,1%"=="2" goto SETUP_MOODLE
if "%LMS_CHOICE:~0,1%"=="3" goto SETUP_BOTH
if "%LMS_CHOICE:~0,1%"=="4" goto MENU_PRINCIPAL

echo.
echo [!] Opção inválida!
timeout /t 2 >nul
goto CONFIG_LMS_PROMPT


rem --- Configuração Canvas ---
:SETUP_CANVAS
cls
echo ==================================================================
echo                 Configuração do Canvas LMS
echo ==================================================================
echo.
echo • URL Base da Instituição (Ex: https://puc-rio.instructure.com)
set "NEW_CANVAS_URL="
set /p "NEW_CANVAS_URL=URL Base [Enter para manter https://puc-rio.instructure.com]: "
if "%NEW_CANVAS_URL%"=="" set "NEW_CANVAS_URL=https://puc-rio.instructure.com"
call :SET_ENV_VAR "CANVAS_BASE_URL" "%NEW_CANVAS_URL%"

echo.
echo • Modo Mock da PUC-Rio (Simulação Realista para Testes Locais)
echo   Permite testar resolução de listas e envio ao Canvas sem precisar
echo   de credenciais reais ou token do portal acadêmico.
set "OPT_MOCK="
set /p "OPT_MOCK=Deseja ativar o Modo Mock? (S/N) [Padrão: S]: "

if /i "%OPT_MOCK:~0,1%"=="n" (
    call :SET_ENV_VAR "CANVAS_MOCK" "false"
    call :SET_ENV_VAR "CANVAS_MOCK_MODE" "False"
    echo.
    echo • Token de Acesso da API do Canvas (Bearer Token)
    echo   (Gere em sua conta Canvas: Perfil -^> Configurações -^> Novo Token de Acesso)
    set "NEW_CANVAS_TOKEN="
    set /p "NEW_CANVAS_TOKEN=Token da API do Canvas: "
    if not "%NEW_CANVAS_TOKEN%"=="" (
        call :SET_ENV_VAR "CANVAS_API_TOKEN" "%NEW_CANVAS_TOKEN%"
    )
) else (
    call :SET_ENV_VAR "CANVAS_MOCK" "true"
    call :SET_ENV_VAR "CANVAS_MOCK_MODE" "True"
    call :SET_ENV_VAR "CANVAS_API_TOKEN" "mock_token"
    echo.
    echo   [OK] Modo Mock PUC-Rio ativado com sucesso!
    echo   Disciplinas simuladas: INF1005, INF1025, MAT1161, ENG1000.
)

if not "%LMS_CHOICE:~0,1%"=="3" (
    call :SET_ENV_VAR "LMS_PROVIDER" "canvas"
    echo.
    echo [OK] Provedor Canvas LMS configurado com sucesso!
    timeout /t 2 >nul
    if "%MODO_GUIADO%"=="1" goto CONCLUSAO_GUIADA
    goto MENU_PRINCIPAL
)
exit /b 0


rem --- Configuração Moodle ---
:SETUP_MOODLE
cls
echo ==================================================================
echo                   Configuração do Moodle
echo ==================================================================
echo.
echo • URL Base do Moodle (Ex: https://virtual.ufmg.br)
set "NEW_MOODLE_URL="
set /p "NEW_MOODLE_URL=URL do Moodle [Enter para manter https://virtual.ufmg.br]: "
if "%NEW_MOODLE_URL%"=="" set "NEW_MOODLE_URL=https://virtual.ufmg.br"
call :SET_ENV_VAR "MOODLE_BASE_URL" "%NEW_MOODLE_URL%"

echo.
echo • Modo de Autenticação:
echo   [1] Credenciais (Login headless automático via Playwright)
echo   [2] Cookies de Sessão (Login interativo no desktop)
set "AUTH_CHOICE="
set /p "AUTH_CHOICE=Escolha o modo (1 ou 2) [Padrão: 1]: "

if "%AUTH_CHOICE:~0,1%"=="2" (
    call :SET_ENV_VAR "AUTH_MODE" "cookies"
    echo   [OK] Modo de autenticação definido como 'cookies'.
) else (
    call :SET_ENV_VAR "AUTH_MODE" "credentials"
    echo.
    echo • Usuário / Matrícula do Moodle
    set "NEW_M_USER="
    set /p "NEW_M_USER=Usuário do Moodle [Enter para manter]: "
    if not "%NEW_M_USER%"=="" (
        call :SET_ENV_VAR "MOODLE_USERNAME" "%NEW_M_USER%"
    )

    echo.
    echo • Senha do Portal Acadêmico
    set "NEW_M_PASS="
    set /p "NEW_M_PASS=Senha do Moodle [Enter para manter]: "
    if not "%NEW_M_PASS%"=="" (
        call :SET_ENV_VAR "MOODLE_PASSWORD" "%NEW_M_PASS%"
    )
)

if not "%LMS_CHOICE:~0,1%"=="3" (
    call :SET_ENV_VAR "LMS_PROVIDER" "moodle"
    echo.
    echo [OK] Provedor Moodle configurado com sucesso!
    timeout /t 2 >nul
    if "%MODO_GUIADO%"=="1" goto CONCLUSAO_GUIADA
    goto MENU_PRINCIPAL
)
exit /b 0


rem --- Configuração Ambas ---
:SETUP_BOTH
call :SETUP_CANVAS
call :SETUP_MOODLE
call :SET_ENV_VAR "LMS_PROVIDER" "multi"
echo.
echo ==================================================================
echo  [OK] Modo Multi-LMS ativado! O LumiBot sincronizará Canvas e Moodle.
echo ==================================================================
timeout /t 2 >nul
if "%MODO_GUIADO%"=="1" goto CONCLUSAO_GUIADA
goto MENU_PRINCIPAL


rem ------------------------------------------------------------------
rem Módulo 3: Passo a Passo Completo Guiado
rem ------------------------------------------------------------------
:CONFIG_GUIADA
set "MODO_GUIADO=1"
goto CONFIG_DISCORD_IA

:CONCLUSAO_GUIADA
cls
echo ==================================================================
echo           🎉 Configuração do LumiBot Concluída com Sucesso!
echo ==================================================================
echo.
echo O seu assistente acadêmico está pronto para entrar em ação!
echo.
echo Próximos passos recomendados:
echo  1. Execute 'iniciar.bat' para colocar o LumiBot online.
echo  2. No Discord, teste os comandos:
echo     • /tarefas    - Para listar suas entregas do Canvas e Moodle
echo     • /resolver   - Para gerar rascunho de IA com 1 clique
echo     • /materiais  - Para consultar as apostilas das matérias
echo.
echo ==================================================================
echo Pressione qualquer tecla para retornar ao menu principal...
pause >nul
set "MODO_GUIADO=0"
goto MENU_PRINCIPAL


rem ------------------------------------------------------------------
rem Módulo 4: Abrir Interface Gráfica Web
rem ------------------------------------------------------------------
:ABRIR_WEB
cls
echo ==================================================================
echo               Iniciando Painel Web do LumiBot...
echo ==================================================================
echo.
echo Abrindo o servidor de configuração visual no seu navegador padrão...
echo.
if exist "config_gui.py" (
    %PYTHON_EXEC% config_gui.py
) else (
    echo [!] Arquivo config_gui.py não encontrado.
    pause
)
goto MENU_PRINCIPAL


rem ------------------------------------------------------------------
rem Sub-rotinas Utilitárias para Manipulação Segura do .env
rem ------------------------------------------------------------------
:SET_ENV_VAR
%PYTHON_EXEC% -c "import sys; from pathlib import Path; k, v = sys.argv[1], sys.argv[2]; p = Path('.env'); lines = p.read_text(encoding='utf-8', errors='ignore').splitlines() if p.exists() else []; found = False; out = []; [out.append(f'{k}={v}') if l.strip().startswith(f'{k}=') and not (found := True) else out.append(l) for l in lines]; not found and out.append(f'{k}={v}'); p.write_text('\n'.join(out) + '\n', encoding='utf-8')" "%~1" "%~2" >nul 2>&1
exit /b 0

:SAIR
cls
echo ==================================================================
echo         Até logo! Execute 'iniciar.bat' para ligar o LumiBot.
echo ==================================================================
echo.
exit /b 0
