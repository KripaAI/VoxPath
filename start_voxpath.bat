@echo off
setlocal
rem Starts the VoxPath backend and frontend, each in its own window, then opens the app.
rem Close those two windows (or press Ctrl+C in them) to stop the servers.

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"

if not exist "%BACKEND%\.env" (
    echo [ERROR] backend\.env is missing. It must contain OPENAI_API_KEY, DATABASE_URL, RAPIDAPI_KEY and JWT_SECRET.
    pause
    exit /b 1
)

rem --- First run only: rebuild the Python environment if it was deleted ---
if not exist "%BACKEND%\.venv\Scripts\python.exe" (
    echo Setting up the backend Python environment ^(first run only, takes a few minutes^)...
    where python >nul 2>nul || (
        echo [ERROR] Python 3.11 was not found on PATH.
        pause
        exit /b 1
    )
    python -m venv "%BACKEND%\.venv" || (pause & exit /b 1)
    "%BACKEND%\.venv\Scripts\python.exe" -m pip install -r "%BACKEND%\requirements.txt" || (pause & exit /b 1)
)

rem --- First run only: reinstall frontend libraries if they were deleted ---
if not exist "%FRONTEND%\node_modules" (
    echo Installing frontend libraries ^(first run only, takes a few minutes^)...
    pushd "%FRONTEND%"
    call npm install || (popd & pause & exit /b 1)
    popd
)

rem Skip a server whose port is already taken, so a second double-click doesn't start duplicates.
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if errorlevel 1 (
    echo Starting backend on http://127.0.0.1:8000 ...
    start "VoxPath Backend" /D "%BACKEND%" cmd /k ".venv\Scripts\python.exe main.py"
) else (
    echo Backend is already running on port 8000 - not starting another one.
)

netstat -ano | findstr /R /C:":3000 .*LISTENING" >nul
if errorlevel 1 (
    echo Starting frontend on http://localhost:3000 ...
    start "VoxPath Frontend" /D "%FRONTEND%" cmd /k "npm run dev"
) else (
    echo Frontend is already running on port 3000 - not starting another one.
)

echo Waiting for the backend to be ready...
set /a TRIES=0
:wait_backend
set /a TRIES+=1
if %TRIES% gtr 90 (
    echo [WARNING] Backend did not respond after 3 minutes. Check the "VoxPath Backend" window for errors.
    goto wait_frontend_start
)
curl -s -f http://127.0.0.1:8000/health >nul 2>nul || (ping -n 3 127.0.0.1 >nul & goto wait_backend)
echo Backend is ready.

:wait_frontend_start
echo Waiting for the frontend to be ready...
set /a TRIES=0
:wait_frontend
set /a TRIES+=1
if %TRIES% gtr 90 (
    echo [WARNING] Frontend did not respond after 3 minutes. Check the "VoxPath Frontend" window for errors.
    pause
    exit /b 1
)
curl -s -f -o nul http://localhost:3000/ 2>nul || (ping -n 3 127.0.0.1 >nul & goto wait_frontend)
echo Frontend is ready.

start "" http://localhost:3000/chat
echo.
echo VoxPath is running. Opened http://localhost:3000/chat in your browser.
echo To stop it, close the "VoxPath Backend" and "VoxPath Frontend" windows.
ping -n 6 127.0.0.1 >nul
endlocal
