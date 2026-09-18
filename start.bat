@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ======================================================================
echo           ABOFAB COMMUNITY - AI Studio ^& Autonomous Bridge
echo ======================================================================
echo.

set "PY="
set "PY_BASE="

:: 1. Проверяем локальный venv проекта
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import fastapi, uvicorn" >nul 2>nul
    if not errorlevel 1 (
        set "PY=%~dp0.venv\Scripts\python.exe"
        goto :LAUNCH
    )
)

:: 2. Ищем Python, где библиотеки УЖЕ установлены
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" (
        "%%D\python.exe" -c "import fastapi, uvicorn" >nul 2>nul
        if not errorlevel 1 (
            set "PY=%%D\python.exe"
            goto :LAUNCH
        )
    )
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python -c "import fastapi, uvicorn" >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
        goto :LAUNCH
    )
)

:: 3. Если готового окружения нет, ищем базовый Python 3.10+ для создания .venv
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" (
        "%%D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set "PY_BASE=%%D\python.exe"
            goto :SETUP_VENV
        )
    )
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PY_BASE=py -3"
        goto :SETUP_VENV
    )
)

for /d %%D in ("%ProgramFiles%\Python3*", "%ProgramFiles(x86)%\Python3*") do (
    if exist "%%D\python.exe" (
        "%%D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set "PY_BASE=%%D\python.exe"
            goto :SETUP_VENV
        )
    )
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PY_BASE=python"
        goto :SETUP_VENV
    )
)

:: 4. Если Python не найден вообще - автоустановка
echo [!] Python 3.10+ не найден на компьютере.
echo [*] Попытка автоматической установки Python 3.12...
echo.

where winget >nul 2>nul
if %errorlevel% equ 0 (
    echo [*] Установка через winget...
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
) else (
    echo [*] Загрузка установщика Python...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object System.Net.WebClient).DownloadFile('https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe', '$env:TEMP\python_installer.exe')"
    if exist "%TEMP%\python_installer.exe" (
        echo [*] Тихая установка Python 3.12...
        "%TEMP%\python_installer.exe" /passive InstallAllUsers=0 PrependPath=1 Include_pip=1
        del "%TEMP%\python_installer.exe" >nul 2>nul
    )
)

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" set "PY_BASE=%%D\python.exe"
)
if not defined PY_BASE (
    where python >nul 2>nul
    if %errorlevel% equ 0 set "PY_BASE=python"
)

if not defined PY_BASE (
    echo [X] Не удалось автоматически установить Python.
    echo     Пожалуйста, установите Python 3.10+ с https://www.python.org/
    echo     Обязательно включите галочку 'Add Python to PATH' при установке.
    pause
    exit /b 1
)

:SETUP_VENV
echo [*] Базовый Python: %PY_BASE%
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [*] Создание изолированного виртуального окружения .venv...
    %PY_BASE% -m venv "%~dp0.venv"
)
set "PY=%~dp0.venv\Scripts\python.exe"
echo [*] Установка необходимых библиотек из requirements.txt...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
goto :LAUNCH

:LAUNCH
echo [OK] Используется Python: %PY%
echo.

:: 5. Проверка библиотек перед стартом
"%PY%" -c "import fastapi, uvicorn, playwright, yaml, curl_cffi, httpx, brotli, h2" >nul 2>nul
if errorlevel 1 (
    echo [*] Доустановка недостающих зависимостей...
    "%PY%" -m pip install -r requirements.txt
)

:: 6. Проверка Node.js
where node >nul 2>nul
if errorlevel 1 (
    echo [*] Node.js не установлен. Автономные инструменты работают нативно через Python.
) else (
    echo [OK] Node.js обнаружен.
)
echo.

:: 7. Запуск
echo ======================================================================
echo  Все сервисы готовы к работе:
echo    * Веб-панель управления:      http://localhost:8000
echo    * Автономный API кодинга:     http://localhost:8765/v1 (abofab)
echo    * Чат-интерфейс:             http://localhost:8081/v1
echo ======================================================================
echo.

start "" "http://localhost:8000"

"%PY%" -m uvicorn app:app --host 0.0.0.0 --port 8000
pause
