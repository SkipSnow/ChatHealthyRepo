// ChatHealthyClaudeLogMgmt Windows Service
// T005: Supervisor only. Starts Docker/Kafka, sidecar (PID 1), consumer (PID 2).
//       Monitors both PIDs, restarts on death. Kills children on stop. No business logic.
// T006: AUTO_START — runs on system boot.
// BUG-LOG-002: Must kill child processes on stop/crash. No zombies.

using System.Diagnostics;

namespace ChatHealthyLogService;

public class Worker(ILogger<Worker> logger) : BackgroundService
{
    private const string PythonExe = @"c:\chatHealthy\findCare\.venv\Scripts\python.exe";
    private const string RepoRoot = @"c:\chatHealthy\findCare";
    private const string SidecarScript = @"Code\Shared\ops\kafka\conversation_log_producer.py";
    private const string ConsumerScript = @"Code\Shared\ops\kafka\conversation_log_consumer.py";
    private const string DockerComposePath = @"Code\Shared\ops\kafka\docker-compose.yml";
    private const int HealthCheckIntervalMs = 30_000;  // 30 seconds
    private const int RestartDelayMs = 5_000;          // 5 seconds before restart

    private Process? _sidecarProcess;
    private Process? _consumerProcess;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // Step 1: Ensure Kafka is running in Docker
        await EnsureKafkaRunning(stoppingToken);
        if (stoppingToken.IsCancellationRequested) return;

        // Wait for Kafka to be ready (health check)
        logger.LogInformation("Waiting for Kafka broker to be ready...");
        await Task.Delay(10_000, stoppingToken);

        // Step 2: Start sidecar (PID 1) and consumer (PID 2)
        _sidecarProcess = StartPython(SidecarScript, "Sidecar");
        _consumerProcess = StartPython(ConsumerScript, "Consumer");

        // Step 3: Monitor loop — restart any dead child
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(HealthCheckIntervalMs, stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }

            // Check and restart sidecar
            if (_sidecarProcess == null || _sidecarProcess.HasExited)
            {
                logger.LogWarning("Sidecar (PID 1) died — restarting in {Delay}ms", RestartDelayMs);
                await Task.Delay(RestartDelayMs, stoppingToken);
                _sidecarProcess = StartPython(SidecarScript, "Sidecar");
            }

            // Check and restart consumer
            if (_consumerProcess == null || _consumerProcess.HasExited)
            {
                logger.LogWarning("Consumer (PID 2) died — restarting in {Delay}ms", RestartDelayMs);
                await Task.Delay(RestartDelayMs, stoppingToken);
                _consumerProcess = StartPython(ConsumerScript, "Consumer");
            }
        }
    }

    private async Task EnsureKafkaRunning(CancellationToken ct)
    {
        // Check if Kafka container is running
        var checkPsi = new ProcessStartInfo
        {
            FileName = "docker",
            Arguments = "ps --filter name=chathealthy-kafka --format {{.Status}}",
            UseShellExecute = false,
            RedirectStandardOutput = true,
            CreateNoWindow = true,
            WorkingDirectory = RepoRoot,
        };

        try
        {
            using var checkProcess = Process.Start(checkPsi);
            if (checkProcess == null) throw new Exception("Failed to run docker ps");

            var output = await checkProcess.StandardOutput.ReadToEndAsync(ct);
            await checkProcess.WaitForExitAsync(ct);

            if (!string.IsNullOrWhiteSpace(output) && output.Contains("Up"))
            {
                logger.LogInformation("Kafka container already running");
                return;
            }
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Failed to check Kafka container status");
        }

        // Start Kafka via docker compose
        logger.LogInformation("Starting Kafka via docker compose...");
        var composePsi = new ProcessStartInfo
        {
            FileName = "docker",
            Arguments = $"compose -f \"{DockerComposePath}\" up -d",
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            WorkingDirectory = RepoRoot,
        };

        try
        {
            using var composeProcess = Process.Start(composePsi);
            if (composeProcess == null) throw new Exception("Failed to run docker compose");

            var stdout = await composeProcess.StandardOutput.ReadToEndAsync(ct);
            var stderr = await composeProcess.StandardError.ReadToEndAsync(ct);
            await composeProcess.WaitForExitAsync(ct);

            if (composeProcess.ExitCode == 0)
                logger.LogInformation("Kafka started: {Output}", stdout.Trim());
            else
                logger.LogError("Kafka failed to start: {Error}", stderr.Trim());
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Failed to start Kafka");
        }
    }

    private Process? StartPython(string scriptRelPath, string name)
    {
        var fullPath = Path.Combine(RepoRoot, scriptRelPath);
        var psi = new ProcessStartInfo
        {
            FileName = PythonExe,
            Arguments = $"\"{fullPath}\"",
            UseShellExecute = false,
            RedirectStandardOutput = false,
            RedirectStandardError = false,
            CreateNoWindow = true,
            WorkingDirectory = RepoRoot,
        };

        var process = Process.Start(psi);
        if (process == null)
        {
            logger.LogError("Failed to start {Name}: {Script}", name, scriptRelPath);
            return null;
        }

        logger.LogInformation("{Name} started (PID {Pid}): {Script}", name, process.Id, scriptRelPath);
        return process;
    }

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        logger.LogInformation("StopAsync — killing all child processes");
        KillChild(ref _sidecarProcess, "Sidecar");
        KillChild(ref _consumerProcess, "Consumer");
        await base.StopAsync(cancellationToken);
    }

    public override void Dispose()
    {
        KillChild(ref _sidecarProcess, "Sidecar");
        KillChild(ref _consumerProcess, "Consumer");
        _sidecarProcess?.Dispose();
        _consumerProcess?.Dispose();
        base.Dispose();
    }

    private void KillChild(ref Process? process, string name)
    {
        if (process == null || process.HasExited)
            return;
        try
        {
            process.Kill(entireProcessTree: true);
            logger.LogInformation("{Name} killed (PID {Pid})", name, process.Id);
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Failed to kill {Name} (PID {Pid})", name, process.Id);
        }
    }
}
