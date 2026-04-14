// ChatHealthyClaudeLogManagementWindowsService
// .NET Worker Service that calls the Python agent every 24 hours.
// Implements T001, B004.

using System.Diagnostics;

namespace ChatHealthyLogService;

public class Worker(ILogger<Worker> logger) : BackgroundService
{
    private const string PythonExe = @"c:\chatHealthy\findCare\.venv\Scripts\python.exe";
    private const string AgentScript = @"c:\chatHealthy\findCare\Code\Shared\ops\tools\conversation_log_purge_service.py";

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("ChatHealthyClaudeLogManagementWindowsService started at {time}", DateTimeOffset.Now);

        while (!stoppingToken.IsCancellationRequested)
        {
            logger.LogInformation("Running purge cycle at {time}", DateTimeOffset.Now);

            try
            {
                await RunPythonAgent(stoppingToken);
            }
            catch (Exception ex)
            {
                logger.LogError(ex, "Purge cycle failed");
            }

            // B004: Repeat every 24 hours
            logger.LogInformation("Next cycle in 24 hours");
            await Task.Delay(TimeSpan.FromHours(24), stoppingToken);
        }

        logger.LogInformation("ChatHealthyClaudeLogManagementWindowsService stopped at {time}", DateTimeOffset.Now);
    }

    private async Task RunPythonAgent(CancellationToken stoppingToken)
    {
        var psi = new ProcessStartInfo
        {
            FileName = PythonExe,
            Arguments = $"\"{AgentScript}\" --purge-now",
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

        var stdout = await process.StandardOutput.ReadToEndAsync(stoppingToken);
        var stderr = await process.StandardError.ReadToEndAsync(stoppingToken);

        await process.WaitForExitAsync(stoppingToken);

        if (process.ExitCode == 0)
        {
            logger.LogInformation("Purge cycle completed successfully. Output: {output}", stdout.Trim());
        }
        else
        {
            logger.LogError("Purge cycle failed with exit code {code}. Stderr: {stderr}", process.ExitCode, stderr.Trim());
        }
    }
}
