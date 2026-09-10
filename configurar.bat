@echo off
title Moodle AI Assistant - Painel de Configuração
cd /d "%~dp0"

echo ============================================================
echo   Moodle AI Assistant (UFMG) - Painel de Configuracao
echo ============================================================
echo.
echo Iniciando interface grafica de configuracao do .env...
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" config_gui.py
) else (
    python config_gui.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ocorreu um erro ao iniciar o painel.
    pause
)
