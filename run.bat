@echo off
setlocal

:: ============================================================================
:: Project and Environment Variables
:: ============================================================================
set "PROJECT_DIR=%~dp0"
set "VENV_DIR=venv"
set "PID_FILE_PATH=%PROJECT_DIR%bin\app.pid"
set "PYTHON_EXEC=python"


:: ============================================================================
:: Initial Directory Creation
:: ============================================================================
:: Ensure all necessary base directories exist before any other operations.
if not exist "%PROJECT_DIR%bin" mkdir "%PROJECT_DIR%bin"
if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
if not exist "%PROJECT_DIR%templates" mkdir "%PROJECT_DIR%templates"
if not exist "%PROJECT_DIR%external" mkdir "%PROJECT_DIR%external"
if not exist "%PROJECT_DIR%_ROOT-INDEX_" mkdir "%PROJECT_DIR%_ROOT-INDEX_"
if not exist "%PROJECT_DIR%_ROOT-INDEX_\.assets" mkdir "%PROJECT_DIR%_ROOT-INDEX_\.assets"


:: ============================================================================
:: Argument Parsing and Cleanup
:: ============================================================================
set "CMD_ARGS="
set "CLEAR_LOG=0"
set "CLEAR_BIN=0"
set "CLEAN_SRC=0"
set "END_EARLY=0"

if /i "%~1"=="fresh" (
    echo Fresh start requested.
    set "CLEAR_LOG=1"
    set "CLEAR_BIN=1"
    set "CLEAN_SRC=1"
    shift /1
) else if /i "%~1"=="clear" (
    echo Clear files requested. This will exit after cleaning.
    set "CLEAR_LOG=1"
    set "CLEAR_BIN=1"
    set "CLEAN_SRC=1"
    set "END_EARLY=1"
    shift /1
) else if /i "%~1"=="clog" (
    echo Clear logs requested. This will exit after cleaning.
    set "CLEAR_LOG=1"
    set "END_EARLY=1"
    shift /1
) else if /i "%~1"=="cbin" (
    echo Clear bin requested. This will exit after cleaning.
    set "CLEAR_BIN=1"
    set "END_EARLY=1"
    shift /1
) else if /i "%~1"=="csrc" (
    echo Clean src requested. This will exit after cleaning.
    set "CLEAN_SRC=1"
    set "END_EARLY=1"
    shift /1
) else if /i "%~1"=="flog" (
    echo Deleting logs directory before running...
    set "CLEAR_LOG=1"
    shift /1
) else if /i "%~1"=="fbin" (
    echo Deleting bin directory before running...
    set "CLEAR_BIN=1"
    shift /1
) else if /i "%~1"=="fsrc" (
    echo Cleaning src cache before running...
    set "CLEAN_SRC=1"
    shift /1
) else if /i "%~1"=="help" (
    echo Usage: run.bat ^[fresh^|clear^|clog^|...^] ^[console_command^] ^[console_args^]
    echo.
    echo  Startup Modifiers ^(run before application starts^)^:
    echo    fresh        - Deletes 'bin', 'logs', and cleans src cache.
    echo    flog         - Deletes 'logs' only.
    echo    fbin         - Deletes 'bin' only.
    echo    fsrc         - Cleans src cache only.
    echo.
    echo  Standalone Cleanup Commands ^(exit after running^)^:
    echo    clear        - Same as 'fresh' but exits immediately.
    echo    clog, cbin, csrc - Same as 'f...' commands but exit immediately.
    echo.
    echo  Other:
    echo    help         - Displays this help message.
    echo.
    echo Any other arguments are passed directly to the management console.
    endlocal
    exit /b 0
)

:: Collect all remaining arguments to pass to the Python script
:arg_loop_collect
if "%~1"=="" goto :arg_loop_collect_end
set "CMD_ARGS=%CMD_ARGS% %~1"
shift
goto :arg_loop_collect
:arg_loop_collect_end


:: ============================================================================
:: Setup Check
:: ============================================================================
:: If the PID file exists, we assume the environment is set up and skip the
:: lengthy checks, jumping directly to activating the venv and running the app.
:: The 'cbin' or 'fresh' commands above will have already deleted this file,
:: which correctly forces a full setup on the next run.

if not exist "%PROJECT_DIR%bin\assets" mkdir "%PROJECT_DIR%bin\assets"

if exist "%PID_FILE_PATH%" (
    echo.
    echo PID file found.
    echo Skipping batch argument processing.
    echo Assuming environment is ready.
    goto :run_application
)

echo.
echo PID file not found. Performing full setup...
echo.


:: ============================================================================
:: Cleanup run
:: ============================================================================

if %CLEAR_LOG%==1 (
    if exist "%PROJECT_DIR%logs" (
        echo Deleting files in logs...
        rd /s /q "%PROJECT_DIR%logs"
        mkdir "%PROJECT_DIR%logs"
    )
)

if %CLEAR_BIN%==1 (
    if exist "%PROJECT_DIR%bin" (
        echo Deleting files in bin...
        rd /s /q "%PROJECT_DIR%bin"
        mkdir "%PROJECT_DIR%bin"
    )
)

if %CLEAN_SRC%==1 (
    echo Cleaning Python cache files in src directory...
    for /r "%PROJECT_DIR%src" %%f in (*.pyc) do del /q "%%f" 2>nul
    for /d /r "%PROJECT_DIR%src" %%d in (__pycache__) do rd /s /q "%%d" 2>nul
)

if %END_EARLY%==1 (
    echo Cleanup complete. Exiting.
    endlocal
    exit /b 0
)


:: ============================================================================
:: Full Setup
:: ============================================================================
:: This section only runs if the PID file was not found.

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
)

:: Create a default .env file if it doesn't exist
if not exist "%PROJECT_DIR%.env" (
    echo.
    echo .env file not found. Creating a default one...
    (
        echo #  Public Hostname ^(used by Nginx to identify your site^)
        echo APP_PUBLIC_HOSTNAME="localhost:8080"
        echo.
        echo #  Nginx Settings ^(The public-facing server^)
        echo # Use "0.0.0.0" to listen on all network interfaces ^(for LAN/public access^)
        echo # Use "127.0.0.1" to listen only on the local machine.
        echo NGINX_LISTEN_IP="0.0.0.0"
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
        echo.
        echo #  Python Executable
        echo # Options: "python.exe", "pythonw.exe", or full path to specific Python executable
        echo PYTHON_EXECUTABLE="pythonw.exe"
    ) > "%PROJECT_DIR%.env"
    echo Default .env file created. Please review it before running in production.
)

if not exist "%PROJECT_DIR%%VENV_DIR%" (
    echo Creating virtual environment in "%PROJECT_DIR%%VENV_DIR%"...
    %PYTHON_EXEC% -m venv "%PROJECT_DIR%%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Activating virtual environment for setup...
call "%PROJECT_DIR%%VENV_DIR%\Scripts\activate.bat"

echo Ensuring pip is up to date...
%PYTHON_EXEC% -m pip install --upgrade pip >nul

echo Installing/verifying requirements from reqs.txt...
%PYTHON_EXEC% -m pip install -r "%PROJECT_DIR%reqs.txt"
if %errorlevel% neq 0 (
    echo Failed to install requirements from reqs.txt.
    pause
    exit /b 1
)
echo.


:: ============================================================================
:: Application Execution
:: ============================================================================
:run_application

:: This part runs every time, ensuring the venv is active for the current session.
echo Activating virtual environment...
call "%PROJECT_DIR%%VENV_DIR%\Scripts\activate.bat"

:: Set up Python cache directory
set "PYTHONPYCACHEPREFIX=%PROJECT_DIR%bin\__pycache__"
if not exist "%PYTHONPYCACHEPREFIX%" mkdir "%PYTHONPYCACHEPREFIX%"

echo.
if not "%CMD_ARGS%"=="" (
  echo Executing command:%CMD_ARGS%
) else (
  echo Setup complete. Starting the management console...
)
echo.

:: Run the management console with any collected arguments
%PYTHON_EXEC% -m src.main %CMD_ARGS%

endlocal
exit /b 0
