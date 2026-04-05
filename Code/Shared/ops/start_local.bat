@echo off
REM ChatHealthy.ai — Local SIT Environment
REM Starts all services. Run as Administrator (port 80 needs admin).
REM
REM Port 80   — Static Website
REM Port 5173 — React Frontend (Vite)
REM Port 8000 — FindCare Backend
REM Port 8001 — EvaluateCare Backend

echo === ChatHealthy.ai Local SIT ===
echo.

echo Starting Website on port 80...
start "Website :80" cmd /k "cd /d C:\chatHealthy\findCare && python Code\Shared\ops\local_webserver.py"

echo Starting FindCare Backend on port 8000...
start "FindCare :8000" cmd /k "cd /d C:\chatHealthy\findCare\Code\ConversationalUX\FindCareChat\backend && python main.py"

echo Starting EvaluateCare Backend on port 8001...
start "EvaluateCare :8001" cmd /k "cd /d C:\chatHealthy\findCare\Code\evaluate_care && python app.py"

echo Starting React Frontend on port 5173...
start "React :5173" cmd /k "cd /d C:\chatHealthy\findCare\Code\ConversationalUX\FindCareChat\frontend && npm run dev"

echo.
echo All services starting. Open http://localhost in your browser.
echo.
pause
