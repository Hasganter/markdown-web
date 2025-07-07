@echo off
setlocal

REM Project root directory (where this script is)
set PROJECT_DIR=%~dp0
REM Python virtual environment directory name
set VENV_DIR=venv

REM --- Argument Parsing ---
set "CMD_ARGS="
set "CLEAR_LOG=0"
set "CLEAR_BIN=0"
set "END_EARLY=0"

REM Check the first argument
if /i "%~1"=="fresh" (
    REM Fresh start: delete bin and logs directories
    echo Fresh start requested. Deleting bin and logs directories...
    set "CLEAR_LOG=1"
    set "CLEAR_BIN=1"
    shift /1
) else if /i "%~1"=="clear" (
    REM Clear files: delete bin and logs directories, then exit
    echo Clear files requested. Deleting bin and logs directories...
    set "CLEAR_LOG=1"
    set "CLEAR_BIN=1"
    set "END_EARLY=1"
    shift /1
) else if /i "%~1"=="clog" (
    REM Clear logs: delete only logs directory
    echo Clear logs requested. Deleting logs directory...
    set "CLEAR_LOG=1"
    set "END_EARLY=1"
    shift /1
) else if /i "%~1"=="cbin" (
    REM Clear bin: delete only bin directory
    set "CLEAR_BIN=1"
    set "END_EARLY=1"
    shift /1
) else if /i "%~1"=="flog" (
    REM Clear logs: delete only logs directory
    echo Clear logs requested. Deleting logs directory...
    set "CLEAR_LOG=1"
    shift /1
) else if /i "%~1"=="fbin" (
    REM Clear bin: delete only bin directory
    set "CLEAR_BIN=1"
    shift /1
) else if /i "%~1"=="help" (
    REM Display help message
    echo Usage: run.bat ^[fresh^|clear^|clog^|...^] ^[additional console arguments^]
    echo.
    echo fresh: Deletes bin and logs directories before running.
    echo flog: Deletes only the logs directory before running.
    echo fbin: Deletes only the bin directory before running.
    echo clear: Deletes bin and logs directories before exitting.
    echo clog: Deletes only the logs directory before exitting.
    echo cbin: Deletes only the bin directory before exitting.
    echo help: Displays this help message before exitting.
    echo.
    echo Additional arguments will be passed to the management console.
    endlocal
    exit /b 0
)

REM Collect all remaining arguments for CMD_ARGS
:arg_loop_collect
if "%~1"=="" goto :arg_loop_collect_end
set "CMD_ARGS=%CMD_ARGS% %~1"
shift
goto :arg_loop_collect
:arg_loop_collect_end

if %CLEAR_LOG%==1 (
    if exist "%PROJECT_DIR%logs" (
        echo Deleting files in logs...
        del /q "%PROJECT_DIR%logs\*"
        for /d %%i in ("%PROJECT_DIR%logs\*") do rd /s /q "%%i"
    )
)

if %CLEAR_BIN%==1 (
    if exist "%PROJECT_DIR%bin" (
        echo Deleting files in bin...
        del /q "%PROJECT_DIR%bin\*"
        for /d %%i in ("%PROJECT_DIR%bin\*") do rd /s /q "%%i"
    )
)

if %END_EARLY%==1 (
    echo File deletion complete, ending script.
    endlocal
    exit /b 0
)
REM --- End Argument Parsing ---

echo Checking for Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found in PATH. Checking for python3...
    python3 --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo Neither "python" nor "python3" found. Please install Python 3.9+ and add to PATH.
        pause
        exit /b 1
    ) else (
        echo "python3" found.
        set PYTHON_EXEC=python3
    )
) else (
    echo Python found in PATH.
    set PYTHON_EXEC=python
)

REM Create a default .env file if it doesn't exist
if not exist "%PROJECT_DIR%.env" (
    echo.
    echo .env file not found. Creating a default one...
    (
        echo #  Core Settings 
        echo MYAPP_DOMAIN="localhost:8080"
        echo.
        echo #  Nginx Settings ^(The public-facing server^)
        echo NGINX_HOST="0.0.0.0"
        echo NGINX_PORT="8080"
        echo.
        echo #  Hypercorn ASGI Server Settings ^(The Python application server^)
        echo ASGI_PORT="8000"
        echo # Number of worker processes or threads. 0 = ^(2 * cpu_cores^) + 1.
        echo ASGI_WORKERS="1"
        echo.
        echo #  Ngrok Tunneling ^(optional^)
        echo NGROK_ENABLED="False"
        echo NGROK_AUTHTOKEN=""
        echo.
        echo #  Grafana Loki Observability ^(optional^)
        echo LOKI_ENABLED="False"
        echo LOKI_URL="http://localhost:3100"
        echo # Loki tenant ID, required by many installations. "fake" is a common default.
        echo LOKI_ORG_ID="fake"
        echo.
        echo #  DDoS Protection ^(Handled by Nginx^)
        echo DDOS_PROTECTION_ENABLED="False"
    ) > "%PROJECT_DIR%.env"
    echo Default .env file created. Please review it before running in production.
    echo.
)

REM Check if venv exists, create if not
if not exist "%PROJECT_DIR%%VENV_DIR%" (
    echo Creating virtual environment in "%PROJECT_DIR%%VENV_DIR%"...
    %PYTHON_EXEC% -m venv "%PROJECT_DIR%%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Activating virtual environment...
call "%PROJECT_DIR%%VENV_DIR%\Scripts\activate.bat"

echo Checking if pip is up to date...
%PYTHON_EXEC% -m pip install --upgrade pip >nul 2>&1

echo Installing requirements from reqs.txt...
%PYTHON_EXEC% -m pip install -r "%PROJECT_DIR%reqs.txt" >nul
if %errorlevel% neq 0 (
    echo Failed to install requirements from reqs.txt.
    pause
    exit /b 1
)

REM Create necessary directories if they don't exist
if not exist "%PROJECT_DIR%bin" mkdir "%PROJECT_DIR%bin"
if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
if not exist "%PROJECT_DIR%_ROOT-INDEX_" mkdir "%PROJECT_DIR%_ROOT-INDEX_"
if not exist "%PROJECT_DIR%_ROOT-INDEX_\.assets" mkdir "%PROJECT_DIR%_ROOT-INDEX_\.assets"
if not exist "%PROJECT_DIR%bin\assets" mkdir "%ASSETS_OUTPUT_DIR%bin\assets"
if not exist "%PROJECT_DIR%src\templates" mkdir "%PROJECT_DIR%src\templates"
if not exist "%PROJECT_DIR%external" mkdir "%PROJECT_DIR%external"


set PYTHONPYCACHEPREFIX=%PROJECT_DIR%bin\__pycache__
if not exist "%PYTHONPYCACHEPREFIX%" mkdir "%PYTHONPYCACHEPREFIX%"

echo.
if not "%CMD_ARGS%"=="" (
  echo Executing command: %CMD_ARGS%
) else (
  echo Setup complete. Starting the management console...
)
echo.

if defined CMD_ARGS (
    %PYTHON_EXEC% -m src.main %CMD_ARGS%
) else (
    %PYTHON_EXEC% -m src.main
)

endlocal
