// ChatHealthyClaudeLogMgmt Windows Service
// T011: No business logic. Calls Python main entry point.

using System.Diagnostics;

namespace ChatHealthyLogService;

public class Worker(ILogger<Worker> logger) : BackgroundService
{
    private const string PythonExe = @"c:\chatHealthy\findCare\.venv\Scripts\python.exe";
    private const string MainScript = @"c:\chatHealthy\findCare\Code\Shared\ops\tools\conversation_log_purge_service.py";

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var psi = new ProcessStartInfo
        {
            FileName = PythonExe,
            Arguments = $"\"{MainScript}\"",
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };

        using var process = Process.Start(psi);
        if (process == null)
        {
            logger.LogError("Failed to start Python process");
            return;
        }

        await process.WaitForExitAsync(stoppingToken);
    }
}
