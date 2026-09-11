@echo off
chcp 65001 >nul
title Moodle AI Assistant - Atualizador de Versão
cd /d "%~dp0"

echo ============================================================
echo   🔄 Moodle AI Assistant (UFMG) - Atualizador de Versão
echo ============================================================
echo.

where git >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRO] O comando 'git' não está disponível no PATH do Windows.
    pause
    exit /b 1
)

if not exist ".git" (
    echo [AVISO] Esta pasta não parece ser um repositório git clonado.
    pause
    exit /b 1
)

echo [1/3] Baixando novidades do repositório remoto...
git fetch origin main

echo.
echo [2/3] Aplicando atualizações...
git pull origin main

echo.
echo [3/3] Sincronizando dependências do Python...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    ".venv\Scripts\python.exe" -m playwright install chromium
)

echo.
echo ============================================================
echo   ✔ Assistente atualizado com sucesso!
echo ============================================================
echo.
echo Suas configurações locais (.env) e sua sessão do Moodle foram
echo estritamente preservadas intactas.
echo.
pause
