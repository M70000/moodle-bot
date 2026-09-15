@echo off
chcp 65001 >nul
title LumiBot - Executando
cd /d "%~dp0"

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

rem ------------------------------------------------------------------
rem 1. Verificação Pré-Voo: Arquivo de Configuração (.env)
rem ------------------------------------------------------------------
:CHECK_ENV
if not exist ".env" (
    echo [AVISO] Arquivo de configuração (.env) não encontrado!
    echo O LumiBot precisa das configurações básicas (Discord, LMS e IA) para operar.
    echo.
    set "RUN_CONFIG="
    set /p "RUN_CONFIG=Deseja executar o configurar.bat agora? (S/N) [Padrão: S]: "
    if "%RUN_CONFIG%"=="" set "RUN_CONFIG=S"
    if /i "%RUN_CONFIG%"=="s" (
        echo.
        echo Abrindo o Assistente de Configuração do LumiBot...
        call "%~dp0configurar.bat"
        echo.
        if not exist ".env" (
            echo.
            echo [ERRO] O arquivo .env ainda não foi gerado.
            echo Não é possível inicializar sem o arquivo de configuração.
            pause
            exit /b 1
        )
    ) else (
        echo.
        echo [!] Inicialização cancelada. Execute 'configurar.bat' quando desejar configurar.
        pause
        exit /b 1
    )
)

rem ------------------------------------------------------------------
rem 2. Verificação Pré-Voo: Ambiente Virtual Python
rem ------------------------------------------------------------------
set "PYTHON_EXEC="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXEC=.venv\Scripts\python.exe"
    if exist ".venv\Scripts\activate.bat" (
        call ".venv\Scripts\activate.bat" >nul 2>&1
    )
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set "PYTHON_EXEC=python"
    ) else (
        echo.
        echo [ERRO CRÍTICO] Ambiente virtual .venv ou interpretador Python não encontrado!
        echo Execute primeiro o instalador automatizado:
        echo   1. Execute 'instalar.bat' para criar as dependências necessárias.
        echo.
        pause
        exit /b 1
    )
)

rem ------------------------------------------------------------------
rem 3. Sincronização e Atualização Automática do Repositório (Git)
rem ------------------------------------------------------------------
where git >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    if exist ".git" (
        echo [1/3] Verificando se há atualizações no repositório...
        git fetch origin main --quiet >nul 2>&1
        if %ERRORLEVEL% EQU 0 (
            git pull origin main --quiet >nul 2>&1
            echo       [OK] Repositório sincronizado com a versão mais recente.
        )
    )
)

rem ------------------------------------------------------------------
rem 4. Detecção Visual dos Provedores Ativos (Pre-Flight Checks)
rem ------------------------------------------------------------------
echo.
echo [2/3] Verificando provedores e serviços configurados...

set "PYTHONIOENCODING=utf-8"
%PYTHON_EXEC% -c "from pathlib import Path; p = Path('.env'); lines = p.read_text(encoding='utf-8', errors='ignore').splitlines() if p.exists() else []; cfg = dict(l.strip().split('=', 1) for l in lines if '=' in l and not l.strip().startswith('#')); tok = cfg.get('DISCORD_BOT_TOKEN', ''); tok_st = '[OK] Token configurado' if (tok and tok != 'seu_discord_bot_token_aqui') else '[AVISO] Token não configurado'; prov = cfg.get('AI_PROVIDER', 'gemini').lower(); g_key = cfg.get('GEMINI_API_KEY', ''); ds_key = cfg.get('DEEPSEEK_API_KEY', ''); c_key = cfg.get('ANTHROPIC_API_KEY', ''); ai_st = ('[OK] Chave detectada (' + cfg.get('GEMINI_MODEL', 'gemini-2.5-flash') + ')') if (prov == 'gemini' and g_key) else (('[OK] DeepSeek detectado (' + cfg.get('DEEPSEEK_MODEL', 'deepseek-chat') + ')') if (prov == 'deepseek' and ds_key) else (('[OK] Claude detectado (' + cfg.get('ANTHROPIC_MODEL', 'claude-3-5-sonnet') + ')') if (prov == 'claude' and c_key) else ('[OK] Chave Gemini detectada' if g_key else '[AVISO] Chave de IA não configurada'))); c_tok = cfg.get('CANVAS_API_TOKEN', ''); c_url = cfg.get('CANVAS_BASE_URL', 'https://pucminas.instructure.com'); lms_prov = cfg.get('LMS_PROVIDER', 'multi').lower(); c_active = (lms_prov in ('canvas', 'multi')) and bool(c_tok and c_tok not in ('seu_canvas_token_aqui', '')); c_st = ('ATIVADO (' + c_url + ')') if c_active else 'DESATIVADO'; m_url = cfg.get('MOODLE_BASE_URL', 'https://virtual.ufmg.br'); m_user = cfg.get('MOODLE_USERNAME', ''); m_active = (lms_prov in ('moodle', 'multi')) and bool((m_user and m_user != 'seu_usuario_moodle') or Path('storage/cookies/session.json').exists()); m_st = ('ATIVADO (' + m_url + ')') if m_active else 'DESATIVADO'; ai_label = 'IA Gemini:  ' if prov == 'gemini' else ('IA DeepSeek:' if prov == 'deepseek' else ('IA Claude:  ' if prov == 'claude' else 'IA / Motor: ')); print('=================================================================='); print('[STATUS DE INICIALIZAÇÃO]'); print(' • Discord:     ' + tok_st); print(' • ' + ai_label + ' ' + ai_st); print(' • Canvas LMS:  ' + c_st); print(' • Moodle:      ' + m_st); print('==================================================================')"

rem ------------------------------------------------------------------
rem 5. Execução do Daemon Principal do LumiBot
rem ------------------------------------------------------------------
echo.
echo [3/3] Iniciando o LumiBot... Pressione Ctrl+C para encerrar.
echo ==================================================================
echo.

%PYTHON_EXEC% -m src.scheduler.daemon

set "EXIT_CODE=%ERRORLEVEL%"

rem ------------------------------------------------------------------
rem 6. Tratamento de Crash e Encerramento Amigável
rem ------------------------------------------------------------------
echo.
if %EXIT_CODE% NEQ 0 (
    echo ==================================================================
    echo  [AVISO] O LumiBot foi encerrado com código de erro (%EXIT_CODE%).
    echo.
    echo  Possíveis causas:
    echo  • Token do Discord incorreto ou com Message Content Intent desativada.
    echo  • Sessão do Moodle/Canvas expirada ou portal fora do ar.
    echo  • Falha momentânea de conexão de rede ou rate limit de IA.
    echo.
    echo  Dica: Execute 'configurar.bat' para revisar suas credenciais.
    echo ==================================================================
    echo Pressione qualquer tecla para fechar esta janela...
    pause >nul
) else (
    echo ==================================================================
    echo  [LumiBot] Assistente encerrado com sucesso. Até logo!
    echo ==================================================================
    pause
)
