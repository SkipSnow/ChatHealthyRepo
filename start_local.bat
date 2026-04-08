@echo off
REM ChatHealthy.ai — Local Development Environment Startup
REM Starts all services: Caddy (HTTPS), Vite (React), FindCare backend
REM Usage: start_local.bat
REM
REM Prerequisites:
REM   - CA cert installed in Windows trusted root store
REM   - .env configured in Code/.env
REM   - Node modules installed (npm install in frontend)
REM   - Python venv activated

echo ============================================
echo  ChatHealthy.ai — Local Dev Environment
echo ============================================
echo.

REM Kill zombie processes (DR-009)
echo [1/5] Killing zombie processes on ports 80, 443, 5173, 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":80 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":443 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
taskkill /F /IM caddy.exe >nul 2>&1
echo    Done.
echo.

REM Start Caddy (HTTPS reverse proxy + static files)
REM TypeScript compile check before starting
echo [2/6] TypeScript compile check...
cd Code\ConversationalUX\FindCareChat\frontend
call npx tsc --noEmit >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo    [FAIL] TypeScript compilation errors — fix before running
    call npx tsc --noEmit 2>&1
    pause
    exit /b 1
)
echo    [OK] TypeScript compiles clean.
cd ..\..\..\..
echo.

echo [3/6] Starting Caddy (HTTPS on :443)...
start /B "" "Code\Shared\ops\tools\caddy.exe" run --config "Code\Shared\ops\Caddyfile" >nul 2>&1
timeout /t 2 /nobreak >nul
echo    Caddy started.
echo.

REM Start React frontend (Vite dev server)
echo [4/6] Starting React frontend (Vite on :5173)...
start /B cmd /c "cd Code\ConversationalUX\FindCareChat\frontend && npm run dev" >nul 2>&1
timeout /t 3 /nobreak >nul
echo    Vite started.
echo.

REM Start FindCare backend (uvicorn)
echo [5/7] Starting FindCare backend (uvicorn on :8000)...
start /B cmd /c "cd Code\ConversationalUX\FindCareChat\backend && python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload" >nul 2>&1
timeout /t 3 /nobreak >nul
echo    FindCare backend started.
echo.

REM Start EvaluateCare backend (uvicorn)
echo [6/7] Starting EvaluateCare backend (uvicorn on :8001)...
start /B cmd /c "cd Code && python -m uvicorn evaluate_care.app:app --host 0.0.0.0 --port 8001" >nul 2>&1
timeout /t 3 /nobreak >nul
echo    EvaluateCare backend started.
echo.

REM Verify
echo [7/7] Verifying services...
curl -sk https://localhost/ >nul 2>&1 && echo    [OK] Website on https://localhost || echo    [FAIL] Website
curl -s http://localhost:5173/ >nul 2>&1 && echo    [OK] React on http://localhost:5173 || echo    [FAIL] React
curl -s http://localhost:8000/health >nul 2>&1 && echo    [OK] FindCare on http://localhost:8000 || echo    [FAIL] FindCare
curl -s http://localhost:8001/health >nul 2>&1 && echo    [OK] EvaluateCare on http://localhost:8001 || echo    [FAIL] EvaluateCare
echo.

echo ============================================
echo  All services running. Open https://localhost
echo ============================================
echo.
echo  Press Ctrl+C to stop all services.
pause
