// Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
// Licensed under the FindCare Evaluation License (FEL-1.0).
//
// Supervisor for the conversation-log pipeline.
//
// It supervises actors; it is not in the data path. No utterance passes
// through it. If it dies, the writer still spools and the drain still ships --
// only the alerting goes quiet.
//
// One job: keep the drain running.
//
// It does not read the spool, does not know a retention window, does not
// sweep, does not talk to MongoDB, and raises no alarm -- it runs
// as a service in Session 0 and cannot reach the operator's screen, so every
// alarm it ever raised was invisible. Those rules live in Python:
// conversation_log_sweep.py (retention, run by the drain on the first pass of
// a new day) and conversation_log_alarm.py (telling the operator, invoked by
// the writer, which runs in the operator's own session).
//
// There is no Kafka and no sidecar, and no connection string anywhere: the
// drain authenticates with a certificate through the shared Mongo utility.

using System.Diagnostics;
using System.Text.Json;

namespace ClaudeCodeConversationPersistenceService;

public class Worker(
    ILogger<Worker> logger,
    IHostApplicationLifetime lifetime
) : BackgroundService
{
    // Project root comes from the environment; no fallback. A hardcoded
    // absolute path is forbidden.
    private const string RepoRootEnvVar = "CHATHEALTHY_PROJECT_ROOT";
    private const string DrainScript = @"architecture\ClaudeCodeConversationPersistence\conversation_log_drain.py";

    // Hard-coded, and identical to conversation_log_writer.py. One location,
    // no environment to get wrong.
    private const string SpoolDir = @"C:\tmp\ChatHealthySpool";

    // Our own configuration, shipped beside the binary. Deliberately NOT
    // appsettings.json -- that file is the .NET host's own logging config and
    // belongs to the framework. Mixing ours into it makes it unclear which
    // settings are ours and which are Microsoft's.
    private const string ServiceConfigFile = "chathealthy-log-service.json";

    private const int HealthCheckIntervalMs = 12_000;
    private const int RestartDelayMs = 5_000;




    private const int CircuitBreakerCrashCount = 3;
    private const int CircuitBreakerWindowMs = 300_000;
    private readonly List<DateTime> _drainCrashes = [];

    private string _repoRoot = "";
    private string _drainLogLevel = "INFO";
    private string _drainLogDirectory = "";
    private string _pythonExe = "";
    private Process? _drainProcess;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _repoRoot = Environment.GetEnvironmentVariable(RepoRootEnvVar) ?? "";
        if (string.IsNullOrWhiteSpace(_repoRoot) || !Directory.Exists(_repoRoot))
        {
            logger.LogCritical(
                "{EnvVar} is not set to an existing directory. The supervisor " +
                "cannot locate the drain script and is stopping.", RepoRootEnvVar);
            lifetime.StopApplication();
            return;
        }

        _pythonExe = Path.Combine(_repoRoot, ".venv", "Scripts", "python.exe");
        if (!File.Exists(_pythonExe))
        {
            logger.LogCritical(
                "No interpreter at {PythonExe}. The supervisor cannot spawn " +
                "the drain and is stopping.", _pythonExe);
            lifetime.StopApplication();
            return;
        }

        _drainLogLevel = ReadDrainSetting("logLevel", "INFO");
        _drainLogDirectory = ResolveDrainLogDirectory();
        logger.LogInformation("Drain log level {Level}, log file in {Dir}",
                              _drainLogLevel, _drainLogDirectory);

        Directory.CreateDirectory(SpoolDir);
        logger.LogInformation(
            "Supervisor started. Spool {SpoolDir}, interval {Interval}ms.",
            SpoolDir, HealthCheckIntervalMs);

        _drainProcess = StartDrain();

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

            if (_drainProcess is null || _drainProcess.HasExited)
            {
                if (TripCircuitBreaker(_drainCrashes, "Drain")) return;
                logger.LogWarning("Drain died — restarting in {Delay}ms", RestartDelayMs);
                await Task.Delay(RestartDelayMs, stoppingToken);
                _drainProcess = StartDrain();
            }
        }

        KillDrain();
    }

    /// <summary>
    /// The drain's log level, from our own config file beside the binary.
    /// Absent or unreadable means INFO -- a missing config must not make a
    /// service noisier or quieter than the operator expects.
    /// </summary>
    private string ReadDrainSetting(string name, string fallback)
    {
        var path = Path.Combine(AppContext.BaseDirectory, ServiceConfigFile);
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.TryGetProperty("drain", out var drain) &&
                drain.TryGetProperty(name, out var element))
            {
                var value = element.GetString();
                if (!string.IsNullOrWhiteSpace(value)) return value!;
            }
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Could not read {File}; {Name} stays at {Fallback}",
                              path, name, fallback);
        }
        return fallback;
    }

    /// <summary>
    /// Where the drain writes its log, resolved against the service's own
    /// directory. "." puts the log beside the binary and beside the config
    /// that set it, so both are in one place in the deployed directory.
    /// </summary>
    private string ResolveDrainLogDirectory()
    {
        var configured = ReadDrainSetting("logDirectory", ".");
        var resolved = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, configured));
        Directory.CreateDirectory(resolved);
        return resolved;
    }

    private Process? StartDrain()
    {
        var script = Path.Combine(_repoRoot, DrainScript);
        if (!File.Exists(script))
        {
            logger.LogCritical("Drain script not found at {Script}", script);
            return null;
        }
        var psi = new ProcessStartInfo
        {
            FileName = _pythonExe,
            Arguments = $"\"{script}\"",
            WorkingDirectory = _repoRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardError = true,
        };
        // Explicit, not inherited. The child's log level is a deployed fact
        // from our config file, so it does not depend on what happens to be
        // set in the service account's environment.
        psi.Environment["CH_LOG_LEVEL"] = _drainLogLevel;
        psi.Environment["CH_LOG_DESTINATION"] = _drainLogDirectory;
        try
        {
            var proc = Process.Start(psi);
            // Its stderr is the only account of why it died. Redirecting and
            // not reading it is how a ModuleNotFoundError became three silent
            // crashes and a tripped breaker.
            if (proc is not null)
            {
                proc.ErrorDataReceived += (_, e) =>
                {
                    if (!string.IsNullOrWhiteSpace(e.Data))
                        logger.LogError("drain: {Line}", e.Data);
                };
                proc.BeginErrorReadLine();
            }
            logger.LogInformation("Drain started (PID {Pid})", proc?.Id);
            return proc;
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Failed to start the drain");
            return null;
        }
    }

    private void KillDrain()
    {
        try
        {
            if (_drainProcess is { HasExited: false })
            {
                var pid = _drainProcess.Id;
                _drainProcess.Kill(entireProcessTree: true);
                // The supervisor logs the stop, because the drain cannot. This
                // is a hard kill, so no finally block runs in the child and any
                // stop line it wanted to write is never reached.
                logger.LogInformation("Drain stopped (PID {Pid})", pid);
            }
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Could not stop the drain process");
        }
    }

    /// <summary>
    /// Record a crash and decide whether to halt. Crash-looping means a
    /// non-transient cause -- a missing certificate, a bad interpreter -- and
    /// silent restart-forever hides it.
    /// </summary>
    private bool TripCircuitBreaker(List<DateTime> crashes, string name)
    {
        var now = DateTime.UtcNow;
        crashes.RemoveAll(t => t < now.AddMilliseconds(-CircuitBreakerWindowMs));
        crashes.Add(now);
        if (crashes.Count >= CircuitBreakerCrashCount)
        {
            logger.LogCritical(
                "{Name} crash-looping ({Count} crashes in {WindowMs}ms). Most " +
                "likely its certificate cannot be read from the vault. " +
                "Stopping instead of restarting forever.",
                name, crashes.Count, CircuitBreakerWindowMs);
            lifetime.StopApplication();
            return true;
        }
        return false;
    }
}
