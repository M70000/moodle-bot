@echo off
chcp 65001 >nul
title LumiBot - Executando
cd /d "%~dp0"

echo ==================================================================
echo   LumiBot - Copiloto Academico Multi-LMS
echo   Monitoramento de Disciplinas, Prazos e Notificacoes
echo ==================================================================
echo.

rem ------------------------------------------------------------------
rem 1. Verificacao Pre-Voo: Arquivo de Configuracao (.env)
rem ------------------------------------------------------------------
if exist ".env" goto ENV_OK

echo [AVISO] Arquivo de configuracao .env nao encontrado!
echo O LumiBot precisa das configuracoes basicas para operar.
echo.
set "RUN_CONFIG=S"
set /p "RUN_CONFIG=Deseja abrir o painel de configuracao agora? [S/N, Padrao: S]: "
if /i "%RUN_CONFIG%"=="n" (
    echo [!] Inicializacao cancelada. Execute 'configurar.bat' quando desejar configurar.
    pause
    exit /b 1
)

echo.
echo Abrindo o Painel de Configuracao do LumiBot...
call "%~dp0configurar.bat"
echo.

if not exist ".env" (
    echo [ERRO] O arquivo .env nao foi gerado.
    echo Nao e possivel inicializar sem configuracao.
    pause
    exit /b 1
)

:ENV_OK

rem ------------------------------------------------------------------
rem 2. Verificacao Pre-Voo: Ambiente Virtual Python
rem ------------------------------------------------------------------
set "PYTHON_EXEC="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXEC=.venv\Scripts\python.exe"
    if exist ".venv\Scripts\activate.bat" (
        call ".venv\Scripts\activate.bat" >nul 2>&1
    )
    goto PYTHON_READY
)

where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PYTHON_EXEC=python"
    goto PYTHON_READY
)

where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PYTHON_EXEC=py"
    goto PYTHON_READY
)

echo.
echo [ERRO CRITICO] Ambiente virtual .venv ou Python nao encontrado!
echo Execute primeiro o instalador:
echo   1. Execute 'instalar.bat' para criar as dependencias necessarias.
echo.
pause
exit /b 1

:PYTHON_READY

rem ------------------------------------------------------------------
rem 3. Deteccao Visual dos Provedores Ativos (Pre-Flight Checks)
rem ------------------------------------------------------------------
echo.
echo [1/2] Verificando provedores e servicos configurados...

set "PYTHONIOENCODING=utf-8"
%PYTHON_EXEC% -c "from pathlib import Path; p = Path('.env'); lines = p.read_text(encoding='utf-8', errors='ignore').splitlines() if p.exists() else []; cfg = dict(l.strip().split('=', 1) for l in lines if '=' in l and not l.strip().startswith('#')); tok = cfg.get('DISCORD_BOT_TOKEN', ''); tok_st = '[OK] Token configurado' if (tok and tok != 'seu_discord_bot_token_aqui') else '[AVISO] Token nao configurado'; prov = cfg.get('AI_PROVIDER', 'gemini').lower(); g_key = cfg.get('GEMINI_API_KEY', ''); ds_key = cfg.get('DEEPSEEK_API_KEY', ''); c_key = cfg.get('ANTHROPIC_API_KEY', ''); ai_st = ('[OK] Chave detectada [' + cfg.get('GEMINI_MODEL', 'gemini-2.5-flash') + ']') if (prov == 'gemini' and g_key) else (('[OK] DeepSeek detectado [' + cfg.get('DEEPSEEK_MODEL', 'deepseek-chat') + ']') if (prov == 'deepseek' and ds_key) else (('[OK] Claude detectado [' + cfg.get('ANTHROPIC_MODEL', 'claude-3-5-sonnet') + ']') if (prov == 'claude' and c_key) else ('[OK] Chave Gemini detectada' if g_key else '[AVISO] Chave de IA nao configurada'))); lms_prov = cfg.get('LMS_PROVIDER', 'moodle').strip().lower(); lms_label = 'Canvas LMS (Apenas Canvas)' if lms_prov == 'canvas' else ('Moodle (Apenas Moodle)' if lms_prov == 'moodle' else 'Multi-LMS (Canvas + Moodle)'); c_tok = cfg.get('CANVAS_API_TOKEN', ''); c_url = cfg.get('CANVAS_BASE_URL', 'https://pucminas.instructure.com'); c_active = (lms_prov in ('canvas', 'multi')) and bool(c_tok and c_tok not in ('seu_canvas_token_aqui', '')); c_st = ('ATIVADO [' + c_url + ']') if c_active else ('DESATIVADO (Modo Moodle selecionado)' if lms_prov == 'moodle' else ('DESATIVADO (Token nao configurado)' if not c_tok else 'DESATIVADO')); m_url = cfg.get('MOODLE_BASE_URL', 'https://virtual.ufmg.br'); m_user = cfg.get('MOODLE_USERNAME', ''); m_active = (lms_prov in ('moodle', 'multi')) and bool((m_user and m_user != 'seu_usuario_moodle') or Path('storage/cookies/session.json').exists()); m_st = ('ATIVADO [' + m_url + ']') if m_active else ('DESATIVADO (Modo Canvas selecionado)' if lms_prov == 'canvas' else 'DESATIVADO'); ai_label = 'IA Gemini:  ' if prov == 'gemini' else ('IA DeepSeek:' if prov == 'deepseek' else ('IA Claude:  ' if prov == 'claude' else 'IA / Motor: ')); print('=================================================================='); print('[STATUS DE INICIALIZACAO]'); print(' * Modo LMS:    ' + lms_label); print(' * Canvas LMS:  ' + c_st); print(' * Moodle:      ' + m_st); print(' * Discord:     ' + tok_st); print(' * ' + ai_label + ' ' + ai_st); print('==================================================================')"

rem ------------------------------------------------------------------
rem 4. Execucao do Daemon Principal do LumiBot
rem ------------------------------------------------------------------
echo.
echo [2/2] Iniciando o LumiBot... Pressione Ctrl+C para encerrar.
echo ==================================================================
echo.

%PYTHON_EXEC% -m src.scheduler.daemon

set "EXIT_CODE=%ERRORLEVEL%"

rem ------------------------------------------------------------------
rem 5. Tratamento de Crash e Encerramento Amigavel
rem ------------------------------------------------------------------
echo.
if %EXIT_CODE% NEQ 0 (
    echo ==================================================================
    echo  [AVISO] O LumiBot foi encerrado com codigo de erro [%EXIT_CODE%].
    echo.
    echo  Possiveis causas:
    echo  * Token do Discord incorreto ou com Message Content Intent desativada.
    echo  * Sessao do Moodle/Canvas expirada ou portal fora do ar.
    echo  * Falha momentanea de conexao de rede ou rate limit de IA.
    echo.
    echo  Dica: Execute 'configurar.bat' para revisar suas credenciais.
    echo ==================================================================
    echo Pressione qualquer tecla para fechar esta janela...
    pause >nul
) else (
    echo ==================================================================
    echo  [LumiBot] Assistente encerrado com sucesso. Ate logo!
    echo ==================================================================
    pause
)
