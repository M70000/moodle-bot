@echo off
title Moodle AI Assistant - Atualizador de Versao
cd /d "%~dp0"

echo ============================================================
echo   Moodle AI Assistant UFMG - Atualizador de Versao
echo ============================================================
echo.

where git >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRO] O comando 'git' nao esta disponivel no PATH do Windows.
    pause
    exit /b 1
)

if not exist ".git" (
    echo [AVISO] Esta pasta nao parece ser um repositorio git clonado.
    pause
    exit /b 1
)

echo [1/3] Baixando novidades do repositorio remoto...
git fetch origin main
if %ERRORLEVEL% NEQ 0 (
    echo [AVISO] Nao foi possivel conectar ao repositorio remoto.
    pause
    exit /b 1
)

echo.
echo [2/3] Aplicando atualizacoes...
git pull origin main

echo.
echo [3/3] Sincronizando dependencias do Python...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    ".venv\Scripts\python.exe" -m playwright install chromium
)

echo.
echo ============================================================
echo   Assistente atualizado com sucesso!
echo ============================================================
echo.
echo Suas configuracoes locais .env e sua sessao do Moodle foram
echo estritamente preservadas intactas.
-echo.
pause
