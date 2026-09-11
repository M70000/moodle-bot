@echo off
title Moodle AI Assistant - Assistente em Execucao
cd /d "%~dp0"

echo ============================================================
echo   Moodle AI Assistant UFMG - Inicializando
echo ============================================================
echo.

rem 1. Auto-Updater via Git
where git >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    if exist ".git" (
        echo [1/2] Verificando se ha atualizacoes no repositorio...
        git fetch origin main --quiet >nul 2>&1
        if %ERRORLEVEL% EQU 0 (
            git pull origin main --quiet >nul 2>&1
            echo [OK] Repositorio sincronizado com a versao mais recente.
        )
    )
)

echo.
echo [2/2] Iniciando daemon em segundo plano do Moodle Bot...
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m src.scheduler.daemon
) else (
    echo [ERRO] Ambiente virtual .venv nao encontrado!
    echo Execute primeiro o arquivo instalar.bat para configurar o sistema.
    echo.
    pause
    exit /b 1
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [AVISO] O assistente foi encerrado com codigo de erro %ERRORLEVEL%.
)
echo.
pause
