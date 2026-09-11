@echo off
chcp 65001 >nul
title Moodle AI Assistant - Assistente em Execução
cd /d "%~dp0"

echo ============================================================
echo   🤖 Moodle AI Assistant (UFMG) - Inicializando
echo ============================================================
echo.

:: 1. Verificação Automática de Atualizações (Auto-Updater Seguro)
where git >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    if exist ".git" (
        echo [1/2] Verificando se há atualizações no repositório...
        git fetch origin main --quiet --timeout=5 >nul 2>&1
        if %ERRORLEVEL% EQU 0 (
            git status -uno | findstr /C:"behind" >nul 2>&1
            if %ERRORLEVEL% EQU 0 (
                echo.
                echo [NOVIDADE] Nova versao encontrada! Atualizando assistente...
                git pull origin main --quiet
                if %ERRORLEVEL% EQU 0 (
                    echo ✔ Codigo atualizado com sucesso!
                    echo Atualizando dependencias se necessario...
                    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet >nul 2>&1
                )
            ) else (
                echo ✔ Voce ja esta utilizando a versao mais recente.
            )
        ) else (
            echo [INFO] Servidor offline ou sem conexao git. Prosseguindo localmente...
        )
    )
)

echo.
echo [2/2] Iniciando daemon em segundo plano do Moodle Bot...
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m src.scheduler.daemon
) else (
    echo [ERRO] Ambiente virtual (.venv) nao encontrado!
    echo Execute primeiro o arquivo "instalar.bat" para configurar o sistema.
    echo.
    pause
    exit /b 1
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [AVISO] O assistente foi encerrado ou encontrou um erro.
    pause
)
