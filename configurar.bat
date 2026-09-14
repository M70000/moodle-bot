@echo off
chcp 65001 >nul
title LumiBot - Assistente de Configuração Web
cd /d "%~dp0"

echo ==================================================================
echo     __    __  ____  ________  ____  ____  ______
echo    / /   / / / /  ^|/  /  _/  / __ )/ __ \/_  __/
echo   / /   / / / / /^|_/ // /   / __  / / / / / /   
echo  / /___/ /_/ / /  / // /   / /_/ / /_/ / / /    
echo /_____/\____/_/  /_/___/  /_____/\____/ /_/     
echo.
echo           💡 LumiBot • Central de Configurações Web
echo ==================================================================
echo.
echo [+] Inicializando o Painel de Controle e Configuração...
echo     Todas as opções (Discord, IA, Canvas LMS e Moodle) estão
echo     centralizadas na interface visual do seu navegador.
echo.
echo     Pressione Ctrl+C a qualquer momento para encerrar o painel.
echo ==================================================================
echo.

rem ------------------------------------------------------------------
rem 1. Inicialização Básica do Arquivo .env
rem ------------------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
    ) else (
        type nul > ".env"
    )
)

rem ------------------------------------------------------------------
rem 2. Detecção e Ativação do Ambiente Virtual Python
rem ------------------------------------------------------------------
set "PYTHON_EXEC=python"
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXEC=.venv\Scripts\python.exe"
    if exist ".venv\Scripts\activate.bat" (
        call ".venv\Scripts\activate.bat" >nul 2>&1
    )
)

rem ------------------------------------------------------------------
rem 3. Abertura do Painel Web Interativo
rem ------------------------------------------------------------------
%PYTHON_EXEC% config_gui.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ==================================================================
    echo [AVISO] O Painel Web foi encerrado com código (%ERRORLEVEL%).
    echo ==================================================================
    pause
) else (
    echo.
    echo ==================================================================
    echo [LumiBot] Painel de Configurações finalizado com sucesso.
    echo           Execute 'iniciar.bat' para colocar o bot em execução!
    echo ==================================================================
)
