// ChatHealthyClaudeLogMgmt Windows Service
// T005: Supervisor only. Starts Docker/Kafka, sidecar (PID 1), consumer (PID 2).
//       Monitors both PIDs, restarts on death. Kills children on stop. No business logic.
// T006: AUTO_START — runs on system boot.
// BUG-LOG-002: Must kill child processes on stop/crash. No zombies.
// v2.2 Part A — secret read from HKLM\SOFTWARE\ChatHealthy\Secrets and
//       injected into the Python child's process environment block.
//       Removes the BUG-012 machine-scope env var dependency.

using System.Diagnostics;
using Microsoft.Win32;

namespace ChatHealthyLogService;

public class Worker(
    ILogger<Worker> logger,
    IHostApplicationLifetime lifetime
) : BackgroundService
{
    // Per BUG-002: project root is resolved from CHATHEALTHY_PROJECT_ROOT;
    // no fallback. If the env var is not set, the supervisor logs critical
    // and stops the host. Hardcoded absolute paths are forbidden.
    private const string RepoRootEnvVar = "CHATHEALTHY_PROJECT_ROOT";
    private const string SidecarScript = @"architecture\Conversation-logPipeline\conversation_log_producer.py";
    private const string ConsumerScript = @"architecture\Conversation-logPipeline\conversation_log_consumer.py";
    private const string DockerComposePath = @"architecture\Conversation-logPipeline\docker-compose.yml";

    // v2.2 Part A — secret storage. The C# service runs as LocalSystem
    // and reads the registry directly via Microsoft.Win32.Registry. The
    // ACL on the key restricts read access to SYSTEM + Administrators.
    private const string SecretRegistryPath = @"SOFTWARE\ChatHealthy\Secrets";
    private const string SecretValueName = "MONGO_FRONTEND_connectionString";
    private const int HealthCheckIntervalMs = 30_000;  // 30 seconds
    private const int RestartDelayMs = 5_000;          // 5 seconds before restart

    // v2.2 Part B 7.3 circuit breaker. If the same child has crashed
    // CircuitBreakerCrashCount times within CircuitBreakerWindowMs the
    // supervisor stops itself with a critical log naming the registry
    // key. Crash-looping indicates a non-transient cause (credential
    // drift, bad Python on disk, etc.) and silent restart-forever
    // defeats the rotation-as-operational-response model.
    // Window is sized against the supervisor's crash-loop cadence:
    //   Mongo serverSelectionTimeoutMS = 5s
    //   + HealthCheckIntervalMs = 30s
    //   + RestartDelayMs = 5s
    //   ≈ 40s per crash cycle.
    // 5 minutes comfortably catches 3 cycles (~2 minutes) without
    // tripping on a single transient blip.
    private const int CircuitBreakerCrashCount = 3;
    private const int CircuitBreakerWindowMs = 300_000;
    private readonly List<DateTime> _sidecarCrashes = [];
    private readonly List<DateTime> _consumerCrashes = [];

    private string _repoRoot = "";
    private string _pythonExe = "";
    private string _mongoConnectionString = "";
    private Process? _sidecarProcess;
    private Process? _consumerProcess;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // Bulletproof prerequisite validation (BUG-002): env var present,
        // directory exists, venv python exists. Each failure logs critical
        // and stops the host — no silent fallback.
        _repoRoot = Environment.GetEnvironmentVariable(RepoRootEnvVar) ?? "";
        if (string.IsNullOrWhiteSpace(_repoRoot))
        {
            logger.LogCritical(
                "{EnvVar} environment variable is not set. Set it at the " +
                "Windows Service scope (value: absolute path to the project " +
                "root) and restart the service. Supervisor cannot start " +
                "without it.", RepoRootEnvVar);
            lifetime.StopApplication();
            return;
        }
        if (!Directory.Exists(_repoRoot))
        {
            logger.LogCritical(
                "{EnvVar} points to {Path} which does not exist. Fix the " +
                "env var and restart.", RepoRootEnvVar, _repoRoot);
            lifetime.StopApplication();
            return;
        }
        _pythonExe = Path.Combine(_repoRoot, ".venv", "Scripts", "python.exe");
        if (!File.Exists(_pythonExe))
        {
            logger.LogCritical(
                "Expected Python executable at {Path} does not exist. The " +
                "project venv must be created before the supervisor can " +
                "spawn workers.", _pythonExe);
            lifetime.StopApplication();
            return;
        }
        logger.LogInformation(
            "Supervisor starting: RepoRoot={RepoRoot}, PythonExe={PythonExe}",
            _repoRoot, _pythonExe);

        // v2.2 Part A 5.2 — read the secret from HKLM once at startup.
        // Critical-fail if the key is missing; without it the Python
        // children would fall back to whatever's in their inherited env,
        // which (post Part A migration) is empty for this var.
        _mongoConnectionString = ReadSecretFromRegistry();
        if (string.IsNullOrWhiteSpace(_mongoConnectionString))
        {
            logger.LogCritical(
                @"Missing HKLM\{Path}\{Value}. The Part A migration must " +
                "create this key with the connection string as REG_SZ; " +
                "the C# service cannot supply the MongoDB credential " +
                "without it.",
                SecretRegistryPath, SecretValueName);
            lifetime.StopApplication();
            return;
        }
        logger.LogInformation(
            "Secret read from registry OK (length={Length})",
            _mongoConnectionString.Length);

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
                if (TripCircuitBreaker(_sidecarCrashes, "Sidecar"))
                    return;
                logger.LogWarning("Sidecar (PID 1) died — restarting in {Delay}ms", RestartDelayMs);
                await Task.Delay(RestartDelayMs, stoppingToken);
                _sidecarProcess = StartPython(SidecarScript, "Sidecar");
            }

            // Check and restart consumer
            if (_consumerProcess == null || _consumerProcess.HasExited)
            {
                if (TripCircuitBreaker(_consumerCrashes, "Consumer"))
                    return;
                logger.LogWarning("Consumer (PID 2) died — restarting in {Delay}ms", RestartDelayMs);
                await Task.Delay(RestartDelayMs, stoppingToken);
                _consumerProcess = StartPython(ConsumerScript, "Consumer");
            }
        }
    }

    /// <summary>
    /// v2.2 Part B 7.3 — record a crash and decide whether to halt.
    /// Returns true when the same child has crashed CircuitBreakerCrashCount
    /// times within CircuitBreakerWindowMs; the caller must NOT restart and
    /// must let ExecuteAsync unwind (lifetime.StopApplication is invoked).
    /// </summary>
    private bool TripCircuitBreaker(List<DateTime> crashes, string name)
    {
        var now = DateTime.UtcNow;
        var cutoff = now.AddMilliseconds(-CircuitBreakerWindowMs);
        crashes.RemoveAll(t => t < cutoff);
        crashes.Add(now);
        if (crashes.Count >= CircuitBreakerCrashCount)
        {
            logger.LogCritical(
                "{Name} crash-looping ({Count} crashes in {WindowMs}ms) — " +
                "credential drift suspected; check " +
                @"HKLM\SOFTWARE\ChatHealthy\Secrets" +
                " and the Application Event Log (source " +
                "ChatHealthyLogService.Consumer). Stopping the service " +
                "instead of silent infinite restart.",
                name, crashes.Count, CircuitBreakerWindowMs);
            lifetime.StopApplication();
            return true;
        }
        return false;
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
            WorkingDirectory = _repoRoot,
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
            WorkingDirectory = _repoRoot,
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

    /// <summary>
    /// v2.2 Part A 5.2 — read the MongoDB connection string from
    /// HKLM\SOFTWARE\ChatHealthy\Secrets\MONGO_FRONTEND_connectionString.
    /// Returns empty string if missing; caller decides how to handle.
    /// The ACL on the key (set during Part A migration) restricts read
    /// access to SYSTEM + Administrators only.
    /// </summary>
    private string ReadSecretFromRegistry()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(SecretRegistryPath, writable: false);
            if (key == null) return "";
            return key.GetValue(SecretValueName) as string ?? "";
        }
        catch (Exception ex)
        {
            logger.LogError(ex,
                @"Failed reading HKLM\{Path}\{Value}",
                SecretRegistryPath, SecretValueName);
            return "";
        }
    }

    private Process? StartPython(string scriptRelPath, string name)
    {
        var fullPath = Path.Combine(_repoRoot, scriptRelPath);
        var psi = new ProcessStartInfo
        {
            FileName = _pythonExe,
            Arguments = $"\"{fullPath}\"",
            UseShellExecute = false,
            RedirectStandardOutput = false,
            RedirectStandardError = false,
            CreateNoWindow = true,
            WorkingDirectory = _repoRoot,
        };
        // v2.2 Part A 5.3 — inject the secret into the child process's
        // environment block. UseShellExecute=false makes .NET pre-
        // populate psi.EnvironmentVariables with the parent's current
        // env, so PATH / CHATHEALTHY_PROJECT_ROOT / everything else are
        // preserved. We add ONLY MONGO_FRONTEND_connectionString. The
        // value lives in the child's process memory only; no machine-
        // scope or user-scope env var exists for it.
        psi.EnvironmentVariables[SecretValueName] = _mongoConnectionString;

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
