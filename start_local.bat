@echo off
REM ChatHealthy.ai — Local Development Environment Startup
REM Starts all services: Caddy (HTTPS), Vite (React), FindCare, EvaluateCare, SharedServices
REM Usage: start_local.bat
REM
REM Prerequisites:
REM   - CA cert installed in Windows trusted root store
REM   - .env configured at repo root (.env)
REM   - Node modules installed (npm install at repo root)
REM   - vite.config.ts present at repo root (written by build/deploy --env local)
REM   - Python venv activated

echo ============================================
echo  ChatHealthy.ai — Local Dev Environment
echo ============================================
echo.

REM Kill zombie processes on all ports (DR-009)
echo [1/8] Killing zombie processes on ports 80, 443, 5173, 8000, 8001, 8002...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":80 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":443 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8002 " ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
taskkill /F /IM caddy.exe >nul 2>&1
echo    Done.
echo.

REM TypeScript compile check before starting (tsconfig.json at repo root)
echo [2/8] TypeScript compile check...
call npx tsc --noEmit >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo    [FAIL] TypeScript compilation errors — fix before running
    call npx tsc --noEmit 2>&1
    pause
    exit /b 1
)
echo    [OK] TypeScript compiles clean.
echo.

echo [3/8] Starting Caddy (HTTPS on :443)...
start "" /B "Code\Shared\ops\tools\caddy.exe" run --config "Code\Shared\ops\Caddyfile" >nul 2>&1
timeout /t 2 /nobreak >nul
echo    Caddy started.
echo.

echo [4/8] Starting React frontend (Vite on :5173)...
start "" cmd /c "npm run dev > %TEMP%\chathealthy_vite.log 2>&1"
timeout /t 3 /nobreak >nul
echo    Vite started.
echo.

echo [5/8] Starting FindCare backend (uvicorn on :8000)...
start "" cmd /c "cd FindCare\Code && python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload > %TEMP%\chathealthy_findcare.log 2>&1"
timeout /t 3 /nobreak >nul
echo    FindCare started.
echo.

echo [6/8] Starting EvaluateCare backend (uvicorn on :8001)...
start "" cmd /c "cd evaluateCare\Code && python -m uvicorn app:app --host 0.0.0.0 --port 8001 > %TEMP%\chathealthy_evalcare.log 2>&1"
timeout /t 3 /nobreak >nul
echo    EvaluateCare started.
echo.

echo [7/8] Starting SharedServices backend (uvicorn on :8002)...
start "" cmd /c "cd sharedServices\Code && python -m uvicorn app:app --host 0.0.0.0 --port 8002 --reload > %TEMP%\chathealthy_sharedservices.log 2>&1"
timeout /t 5 /nobreak >nul
echo    SharedServices started.
echo.

REM Verify all services
echo [8/8] Verifying services...
curl -sk https://localhost/ >nul 2>&1 && echo    [OK] Website on https://localhost || echo    [FAIL] Website — check Caddy
curl -s http://localhost:5173/ >nul 2>&1 && echo    [OK] React on http://localhost:5173 || echo    [FAIL] React — check %TEMP%\chathealthy_vite.log
curl -s http://localhost:8000/health >nul 2>&1 && echo    [OK] FindCare on http://localhost:8000 || echo    [FAIL] FindCare — check %TEMP%\chathealthy_findcare.log
curl -s http://localhost:8001/health >nul 2>&1 && echo    [OK] EvaluateCare on http://localhost:8001 || echo    [FAIL] EvaluateCare — check %TEMP%\chathealthy_evalcare.log
curl -s http://localhost:8002/health >nul 2>&1 && echo    [OK] SharedServices on http://localhost:8002 || echo    [FAIL] SharedServices — check %TEMP%\chathealthy_sharedservices.log
echo.

echo ============================================
echo  All services running. Open https://localhost
echo  Logs: %TEMP%\chathealthy_*.log
echo ============================================
echo.
echo  Press Ctrl+C to stop all services.
pause
