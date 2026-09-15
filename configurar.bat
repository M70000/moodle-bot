@echo off
chcp 65001 >nul
title LumiBot - Assistente de Configuracao Web
cd /d "%~dp0"

echo ==================================================================
echo   LumiBot - Central de Configuracoes Web
echo   Seu Copiloto Academico Multi-LMS (Canvas e Moodle)
echo ==================================================================
echo.
echo [+] Inicializando o Painel de Controle e Configuracao...
echo     Todas as opcoes (Discord, IA, Canvas LMS e Moodle) estao
echo     centralizadas na interface visual do seu navegador.
echo.
echo     Pressione Ctrl+C a qualquer momento para encerrar o painel.
echo ==================================================================
echo.

rem ------------------------------------------------------------------
rem 1. Inicializacao Basica do Arquivo .env
rem ------------------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
    ) else (
        type nul > ".env"
    )
)

rem ------------------------------------------------------------------
rem 2. Deteccao e Ativacao do Ambiente Virtual Python
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
echo [ERRO] Python ou ambiente virtual .venv nao encontrado!
echo Execute primeiro o instalador:
echo   1. Execute 'instalar.bat' para criar as dependencias necessarias.
echo.
pause
exit /b 1

:PYTHON_READY

rem ------------------------------------------------------------------
rem 3. Abertura do Painel Web Interativo
rem ------------------------------------------------------------------
%PYTHON_EXEC% config_gui.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ==================================================================
    echo [AVISO] O Painel Web foi encerrado com codigo [%ERRORLEVEL%].
    echo ==================================================================
    pause
) else (
    echo.
    echo ==================================================================
    echo [LumiBot] Painel de Configuracoes finalizado com sucesso.
    echo           Execute 'iniciar.bat' para colocar o bot em execucao!
    echo ==================================================================
    pause
)
