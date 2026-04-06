@echo off
REM ChatHealthy.ai — Local SIT Environment
REM Starts all services. Run as Administrator (port 80 needs admin).
REM
REM Caddy   — Reverse proxy, TLS, mTLS enforcement
REM Port 80/443 — Static Website (via Caddy)
REM Port 5173   — React Frontend (Vite)
REM Port 8000   — FindCare Backend
REM Port 8001   — EvaluateCare Backend
REM Port 8002   — Shared Services
REM
REM Architecture: EPIC-6 Secure Distributed Topology

echo === ChatHealthy.ai Local SIT ===
echo.

REM DR-009: Kill zombie processes on target ports before starting
echo Killing zombie processes...
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":80 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":443 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":8001 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":8002 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
timeout /t 3 /nobreak >nul
echo Done.
echo.

echo Starting Caddy (reverse proxy, TLS, mTLS)...
start "Caddy" cmd /k "cd /d C:\chatHealthy\findCare\Code\Shared\ops && tools\caddy.exe run --config Caddyfile"

echo Starting FindCare Backend on port 8000 (takes ~90s to load)...
start "FindCare :8000" cmd /k "cd /d C:\chatHealthy\findCare\Code\ConversationalUX\FindCareChat\backend && set PORT=8000 && python main.py"

echo Starting EvaluateCare Backend on port 8001...
start "EvaluateCare :8001" cmd /k "cd /d C:\chatHealthy\findCare\Code && python -m evaluate_care.app"

echo Starting Shared Services on port 8002...
start "SharedServices :8002" cmd /k "cd /d C:\chatHealthy\findCare\Code\shared_services && python app.py"

echo Starting React Frontend on port 5173...
start "React :5173" cmd /k "cd /d C:\chatHealthy\findCare\Code\ConversationalUX\FindCareChat\frontend && npm run dev"

echo.
echo All services starting. FindCare takes ~90s to connect to MongoDB.
echo Open http://localhost in your browser.
echo.
echo Services:
echo   Caddy       — reverse proxy (ports 80, 443)
echo   FindCare    — :8000
echo   EvaluateCare — :8001
echo   SharedSvcs  — :8002
echo   React       — :5173
echo.
pause
