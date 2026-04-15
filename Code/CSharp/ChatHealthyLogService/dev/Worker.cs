// ChatHealthyClaudeLogMgmt Windows Service
// T011: No business logic. Calls Python main entry point.
// BUG-LOG-002: Must kill child Python process on stop/crash. No zombies.

using System.Diagnostics;

namespace ChatHealthyLogService;

public class Worker(ILogger<Worker> logger) : BackgroundService
{
    private const string PythonExe = @"c:\chatHealthy\findCare\.venv\Scripts\python.exe";
    private const string MainScript = @"c:\chatHealthy\findCare\Code\Shared\ops\tools\conversation_log_purge_service.py";
    private Process? _childProcess;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var psi = new ProcessStartInfo
        {
            FileName = PythonExe,
            Arguments = $"\"{MainScript}\"",
            UseShellExecute = false,
            RedirectStandardOutput = false,
            RedirectStandardError = false,
            CreateNoWindow = true,
        };

        _childProcess = Process.Start(psi);
        if (_childProcess == null)
        {
            logger.LogError("Failed to start Python process");
            return;
        }

        logger.LogInformation("Python process started (PID {Pid})", _childProcess.Id);

        try
        {
            await _childProcess.WaitForExitAsync(stoppingToken);
        }
        catch (OperationCanceledException)
        {
            // Service is stopping — kill the child
            logger.LogInformation("Service stopping — killing Python process (PID {Pid})", _childProcess.Id);
            KillChild();
        }
    }

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        logger.LogInformation("StopAsync called — ensuring Python process is terminated");
        KillChild();
        await base.StopAsync(cancellationToken);
    }

    public override void Dispose()
    {
        KillChild();
        _childProcess?.Dispose();
        base.Dispose();
    }

    private void KillChild()
    {
        if (_childProcess == null || _childProcess.HasExited)
            return;
        try
        {
            _childProcess.Kill(entireProcessTree: true);
            logger.LogInformation("Python process killed (PID {Pid})", _childProcess.Id);
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Failed to kill Python process (PID {Pid})", _childProcess.Id);
        }
    }
}
