@echo off
title Moodle AI Assistant - Atualizador de Versao
cd /d "%~dp0"

echo ============================================================
echo   Moodle AI Assistant UFMG - Atualizador de Versao
echo ============================================================
echo.

rem 1. Se existir a pasta .git E o comando git estiver disponivel no PATH, atualiza via Git
if exist ".git" (
    where git >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        echo [INFO] Repositorio Git detectado.
        echo [1/3] Baixando novidades do repositorio remoto...
        git fetch origin main
        if %ERRORLEVEL% EQU 0 (
            echo.
            echo [2/3] Aplicando atualizacoes...
            git pull origin main
            goto SYNC_DEPS
        ) else (
            echo [AVISO] Nao foi possivel conectar via Git. Tentando atualizador direto...
        )
    )
)

rem 2. Modo Download ZIP (instalacao direta sem Git)
echo [INFO] Modo Download ZIP detectado (instalacao sem Git).
echo Atualizando o assistente a partir do GitHub...
echo.

rem Localiza o interpretador Python (.venv ou global)
set PYTHON_EXE=
if exist ".venv\Scripts\python.exe" (
    set PYTHON_EXE=".venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% EQU 0 set PYTHON_EXE=python
)

rem Se tiver Python e o script tools\update_zip.py existir, usa o utilitario Python
if defined PYTHON_EXE (
    if exist "tools\update_zip.py" (
        %PYTHON_EXE% tools\update_zip.py
        if %ERRORLEVEL% EQU 0 goto SYNC_DEPS
    )
)

rem Fallback automatico via PowerShell caso o Python nao esteja pronto
echo [1/2] Baixando pacote mais recente do GitHub via PowerShell...
powershell -Command "[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12; $u = 'https://github.com/M70000/moodle-bot/archive/refs/heads/main.zip'; $z = Join-Path $env:TEMP 'moodle_bot_update.zip'; Invoke-WebRequest -Uri $u -OutFile $z; Expand-Archive -Path $z -DestinationPath (Join-Path $env:TEMP 'moodle_bot_extracted') -Force"
if %ERRORLEVEL% NEQ 0 (
    echo [ERRO] Nao foi possivel baixar os arquivos do GitHub. Verifique sua conexao.
    pause
    exit /b 1
)

echo [2/2] Aplicando arquivos atualizados (preservando .env e dados locais)...
powershell -Command "$src = Join-Path $env:TEMP 'moodle_bot_extracted\moodle-bot-main'; $dst = (Get-Item .).FullName; Get-ChildItem -Path $src -Recurse | ForEach-Object { $rel = $_.FullName.Substring($src.Length + 1); if ($rel -notlike '.env*' -and $rel -notlike 'storage\*' -and $rel -notlike '.venv\*' -and $rel -notlike '.git\*' -and $rel -ne 'GEMINI.md') { $target = Join-Path $dst $rel; if ($_.PSIsContainer) { if (!(Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null } } else { Copy-Item -Path $_.FullName -Destination $target -Force } } }"

:SYNC_DEPS
echo.
echo [3/3] Sincronizando dependencias e navegador Playwright...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    ".venv\Scripts\python.exe" -m playwright install chromium
)

echo.
echo ============================================================
echo   Assistente atualizado com sucesso!
echo ============================================================
echo.
echo Suas configuracoes locais (.env), sessao do Moodle e materiais
echo foram estritamente preservados intactos.
echo.
pause
