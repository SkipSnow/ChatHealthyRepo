@echo off
REM ChatHealthy.ai — Local SIT Environment
REM Starts all services. Run as Administrator (port 80 needs admin).
REM
REM Port 80   — Static Website
REM Port 443  — Admin (HTTPS, self-signed)
REM Port 5173 — React Frontend (Vite)
REM Port 8000 — FindCare Backend
REM Port 8001 — EvaluateCare Backend

echo === ChatHealthy.ai Local SIT ===
echo.

REM DR-009: Kill zombie processes on target ports before starting
echo Killing zombie processes...
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":80 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":443 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
FOR /F "tokens=5" %%P IN ('netstat -ano ^| findstr ":8001 " ^| findstr "LISTEN"') DO taskkill /F /PID %%P 2>nul
timeout /t 3 /nobreak >nul
echo Done.
echo.

echo Starting Website on port 80...
start "Website :80" cmd /k "cd /d C:\chatHealthy\findCare && python Code\Shared\ops\local_webserver.py"

echo Starting Admin on port 443 (HTTPS)...
start "Admin :443" cmd /k "cd /d C:\chatHealthy\findCare && python Code\Shared\ops\local_admin_server.py"

echo Starting FindCare Backend on port 8000 (takes ~90s to load)...
start "FindCare :8000" cmd /k "cd /d C:\chatHealthy\findCare\Code\ConversationalUX\FindCareChat\backend && set PORT=8000 && python main.py"

echo Starting EvaluateCare Backend on port 8001...
start "EvaluateCare :8001" cmd /k "cd /d C:\chatHealthy\findCare\Code && python -m evaluate_care.app"

echo Starting React Frontend on port 5173...
start "React :5173" cmd /k "cd /d C:\chatHealthy\findCare\Code\ConversationalUX\FindCareChat\frontend && npm run dev"

echo.
echo All services starting. FindCare takes ~90s to connect to MongoDB.
echo Open http://localhost in your browser.
echo.
pause
