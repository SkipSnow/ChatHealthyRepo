# kill_dev_servers.ps1 — Kill all ChatHealthy dev servers
# Run as: powershell -ExecutionPolicy Bypass -File kill_dev_servers.ps1
#
# Kills: FastAPI (main.py), Vite (node), Python http.server
# Checks ports: 80, 5173, 5174, 7860, 8000, 8080

Write-Host "ChatHealthy Dev Server Cleanup" -ForegroundColor Cyan
Write-Host "==============================" -ForegroundColor Cyan

$ports = @(80, 5173, 5174, 7860, 8000, 8080)
$killed = 0

foreach ($port in $ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $connections) {
        $pid = $conn.OwningProcess
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "Port $port : PID $pid ($($proc.ProcessName)) — killing" -ForegroundColor Yellow
            Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            $killed++
        }
    }
}

# Also kill any stray python/node that might be ours
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)").CommandLine
    if ($cmd -match "main\.py|http\.server|epic_planning") {
        Write-Host "Stray python PID $($_.Id): $cmd — killing" -ForegroundColor Yellow
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $killed++
    }
}

Get-Process node -ErrorAction SilentlyContinue | ForEach-Object {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)").CommandLine
    if ($cmd -match "vite|findcare|FindCareChat") {
        Write-Host "Stray node PID $($_.Id): $cmd — killing" -ForegroundColor Yellow
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $killed++
    }
}

Write-Host ""
if ($killed -eq 0) {
    Write-Host "No dev servers found." -ForegroundColor Green
} else {
    Write-Host "Killed $killed processes." -ForegroundColor Green
}

# Verify ports are free
Write-Host ""
Write-Host "Port check:" -ForegroundColor Cyan
foreach ($port in $ports) {
    $still = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($still) {
        Write-Host "  Port $port : STILL IN USE (may need reboot)" -ForegroundColor Red
    } else {
        Write-Host "  Port $port : free" -ForegroundColor Green
    }
}
