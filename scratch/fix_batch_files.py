"""Script to write instalar.bat, iniciar.bat, and atualizar.bat with proper CRLF endings and clean syntax."""

from pathlib import Path

instalar_content = """@echo off
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
if not exist ".venv\\Scripts\\python.exe" (
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
".venv\\Scripts\\python.exe" -m pip install --upgrade pip --quiet
".venv\\Scripts\\python.exe" -m pip install -r requirements.txt --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [AVISO] Tentando instalar pacotes sem modo silencioso...
    ".venv\\Scripts\\python.exe" -m pip install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo [ERRO] Falha ao instalar requirements.txt.
        pause
        exit /b 1
    )
)

rem 4. Instalacao do Navegador Playwright Chromium
echo.
echo [4/4] Instalando navegador automatizado do Moodle Chromium...
".venv\\Scripts\\python.exe" -m playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    echo [AVISO] Tentativa secundaria do Playwright...
    ".venv\\Scripts\\python.exe" -m playwright install
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
"""

iniciar_content = """@echo off
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

if exist ".venv\\Scripts\\python.exe" (
    ".venv\\Scripts\\python.exe" -m src.scheduler.daemon
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
"""

atualizar_content = """@echo off
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
if exist ".venv\\Scripts\\python.exe" (
    ".venv\\Scripts\\python.exe" -m pip install -r requirements.txt --quiet
    ".venv\\Scripts\\python.exe" -m playwright install chromium
)

echo.
echo ============================================================
echo   Assistente atualizado com sucesso!
echo ============================================================
echo.
echo Suas configuracoes locais .env e sua sessao do Moodle foram
echo estritamente preservadas intactas.
echo.
pause
"""

for fname, text in [('instalar.bat', instalar_content), ('iniciar.bat', iniciar_content), ('atualizar.bat', atualizar_content)]:
    crlf_text = text.replace('\r\n', '\n').replace('\n', '\r\n')
    Path(fname).write_bytes(crlf_text.encode('ascii', errors='replace'))
    print(f"Written {fname} with CRLF ({len(crlf_text.encode('ascii'))} bytes)")
