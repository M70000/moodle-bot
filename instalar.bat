@echo off
title Moodle AI Assistant - Instalador One-Click
cd /d "%~dp0"

echo ============================================================
echo   Moodle AI Assistant UFMG - Instalador One-Click
echo ============================================================
echo.
echo Este instalador configurara todo o ambiente necessario para
echo rodar o assistente academico na sua maquina automaticamente.
echo.

rem 1. Verificacao do Python
echo [1/4] Verificando instalacao do Python...
set PYTHON_CMD=

where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON_CMD=python
) else (
    where py >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set PYTHON_CMD=py
    )
)

if "%PYTHON_CMD%"=="" (
    echo.
    echo [AVISO] Python nao encontrado no sistema!
    echo Tentando instalar Python 3.11 via winget...
    winget install Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [INFO] Baixando instalador oficial do Python 3.11...
        powershell -Command "Write-Host 'Baixando...'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile 'python_setup.exe'"
        if exist "python_setup.exe" (
            echo Instalando Python silenciosamente...
            python_setup.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
            del python_setup.exe >nul 2>&1
        )
    )
    set PYTHON_CMD=python
)

rem 2. Criacao do Ambiente Virtual .venv
echo.
echo [2/4] Configurando ambiente virtual isolado .venv...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [ERRO] Falha ao criar ambiente virtual .venv.
        pause
        exit /b 1
    )
)

rem 3. Instalacao de Dependencias
echo.
echo [3/4] Instalando dependencias e bibliotecas necessarias...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [AVISO] Tentando instalar pacotes sem modo silencioso...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo [ERRO] Falha ao instalar requirements.txt.
        pause
        exit /b 1
    )
)

rem 4. Instalacao do Navegador Playwright Chromium
echo.
echo [4/4] Instalando navegador automatizado do Moodle Chromium...
".venv\Scripts\python.exe" -m playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    echo [AVISO] Tentativa secundaria do Playwright...
    ".venv\Scripts\python.exe" -m playwright install
)

rem 5. Preparacao do .env inicial
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo Arquivo .env inicial criado a partir do modelo.
    )
)

echo.
echo ============================================================
echo   Instalacao concluida com sucesso!
echo ============================================================
echo.
echo Abrindo agora o Painel de Configuracao para conectar
echo seu Moodle, Discord e chave do Gemini...
echo.
ping 127.0.0.1 -n 4 >nul

start "" "configurar.bat"
exit /b 0
