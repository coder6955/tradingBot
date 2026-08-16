Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runnerPath = Join-Path $projectRoot "scripts\run_scheduled_app.py"
$fallbackNotifierPath = Join-Path $projectRoot "scripts\notify_scheduled_failure.py"
$logDirectory = Join-Path $projectRoot "logs"

function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $prefix = "$Key="
    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_.TrimStart().StartsWith($prefix) } |
        Select-Object -Last 1
    if (-not $line) {
        return $null
    }
    return $line.Substring($line.IndexOf("=") + 1).Trim().Trim('"').Trim("'")
}

function Test-LocalTcpPort {
    param(
        [string]$HostName = "127.0.0.1",
        [int]$Port = 3306,
        [int]$TimeoutMilliseconds = 1000
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($connect)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Ensure-XamppMySql {
    param(
        [int]$Port = 3306,
        [int]$StartupTimeoutSeconds = 60
    )
    if (Test-LocalTcpPort -Port $Port) {
        return "already_running"
    }

    $candidateRoots = @()
    if ($env:XAMPP_ROOT) {
        $candidateRoots += $env:XAMPP_ROOT
    }
    $candidateRoots += @("C:\xamppp", "C:\xampp")

    $xamppRoot = $candidateRoots |
        Where-Object {
            Test-Path -LiteralPath (Join-Path $_ "mysql\bin\mysqld.exe") -PathType Leaf
        } |
        Select-Object -First 1
    if (-not $xamppRoot) {
        throw "MySQL is unavailable on port $Port and no XAMPP MySQL installation was found"
    }

    $mysqlExecutable = Join-Path $xamppRoot "mysql\bin\mysqld.exe"
    $mysqlConfig = Join-Path $xamppRoot "mysql\bin\my.ini"
    if (-not (Test-Path -LiteralPath $mysqlConfig -PathType Leaf)) {
        throw "XAMPP MySQL configuration was not found"
    }

    Start-Process `
        -FilePath $mysqlExecutable `
        -ArgumentList @("--defaults-file=$mysqlConfig", "--standalone") `
        -WorkingDirectory $xamppRoot `
        -WindowStyle Hidden |
        Out-Null

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        if (Test-LocalTcpPort -Port $Port) {
            return "started"
        }
    } while ((Get-Date) -lt $deadline)

    throw "XAMPP MySQL did not become reachable on port $Port within $StartupTimeoutSeconds seconds"
}

function Send-FallbackAlert {
    param([string]$Reason)
    if (
        (Test-Path -LiteralPath $pythonPath -PathType Leaf) -and
        (Test-Path -LiteralPath $fallbackNotifierPath -PathType Leaf)
    ) {
        try {
            & $pythonPath $fallbackNotifierPath --reason $Reason | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return
            }
        }
        catch {
            # Fall through to the Python-independent Telegram request.
        }
    }
    $dotenvPath = Join-Path $projectRoot ".env"
    $telegramToken = if ($env:TELEGRAM_BOT_TOKEN) {
        $env:TELEGRAM_BOT_TOKEN
    } else {
        Get-DotEnvValue -Path $dotenvPath -Key "TELEGRAM_BOT_TOKEN"
    }
    $telegramChat = if ($env:TELEGRAM_CHAT_ID) {
        $env:TELEGRAM_CHAT_ID
    } else {
        Get-DotEnvValue -Path $dotenvPath -Key "TELEGRAM_CHAT_ID"
    }
    if (-not $telegramToken -or -not $telegramChat) {
        return
    }
    $safeReason = ($Reason -replace "[`r`n]", " ")
    if ($safeReason.Length -gt 240) {
        $safeReason = $safeReason.Substring(0, 240)
    }
    $message = "Bank Nifty app PROCESS FAILURE.`nTime: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')`nIssue: $safeReason`nCheck the scheduled-app logs."
    try {
        Invoke-RestMethod `
            -Method Post `
            -Uri "https://api.telegram.org/bot$telegramToken/sendMessage" `
            -Body @{ chat_id = $telegramChat; text = $message } `
            -TimeoutSec 8 |
            Out-Null
    }
    catch {
        # A dead network cannot report its own failure; the lifecycle log remains.
    }
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Send-FallbackAlert -Reason "Project Python was not found"
    throw "Project Python was not found at $pythonPath"
}
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    Send-FallbackAlert -Reason "Scheduled Python runner was not found"
    throw "Scheduled Python runner was not found at $runnerPath"
}
if (-not (Test-Path -LiteralPath $fallbackNotifierPath -PathType Leaf)) {
    Send-FallbackAlert -Reason "Scheduled failure notifier was not found"
    throw "Scheduled failure notifier was not found at $fallbackNotifierPath"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$runId = Get-Date -Format "yyyy-MM-dd-HHmmss"
$lifecycleLog = Join-Path $logDirectory "scheduled-app-lifecycle.log"
$standardOutputLog = Join-Path $logDirectory "scheduled-app-$runId.stdout.log"
$standardErrorLog = Join-Path $logDirectory "scheduled-app-$runId.stderr.log"

Set-Location -LiteralPath $projectRoot
"[{0}] Scheduled trading app starting." -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss K") |
    Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8

$healthyAppDetected = $false
try {
    $runningHealth = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8000/health" `
        -TimeoutSec 5
    $healthyAppDetected = $runningHealth.status -eq "ok"
    $runningAutomation = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8000/automation/status" `
        -TimeoutSec 5
    $tokenFile = Join-Path $projectRoot "access_token.txt"
    $storedTokenMetadata = Get-Content -LiteralPath $tokenFile -Raw | ConvertFrom-Json
    $today = Get-Date -Format "yyyy-MM-dd"
    if (
        $runningHealth.status -eq "ok" -and
        $storedTokenMetadata.trading_date -eq $today -and
        $runningAutomation.execution_profile -eq "scheduled_run"
    ) {
        "[{0}] A healthy app with today's Kite token is already running; duplicate start skipped." -f (
            Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
        ) | Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8
        exit 0
    }
    if ($runningHealth.status -eq "ok") {
        $profileConflict = "Port 8000 is owned by another app process that is not today's scheduled runtime; scheduled start was blocked"
        Send-FallbackAlert -Reason $profileConflict
        throw $profileConflict
    }
}
catch {
    if ($healthyAppDetected) {
        if ($_.Exception.Message -notlike "Port 8000 is owned*") {
            $profileConflict = "Port 8000 is owned by another app process and its scheduled ownership could not be verified"
            Send-FallbackAlert -Reason $profileConflict
        }
        throw
    }
    # No healthy local app answered, so continue with a normal scheduled start.
}

$env:AUTOMATION_STOP_AFTER_AFTER_MARKET_COMPLETE = "true"
$env:SCHEDULED_RUN_EXIT_AFTER_COMPLETE = "true"

try {
    $mysqlStartupResult = Ensure-XamppMySql
    "[{0}] XAMPP MySQL prerequisite: {1}." -f (
        Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
    ), $mysqlStartupResult | Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8
}
catch {
    $databaseFailure = "Scheduled launcher could not prepare XAMPP MySQL: $($_.Exception.Message)"
    "[{0}] {1}" -f (
        Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
    ), $databaseFailure | Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8
    Send-FallbackAlert -Reason $databaseFailure
    throw
}

try {
    $appProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @($runnerPath, "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $standardOutputLog `
        -RedirectStandardError $standardErrorLog `
        -Wait `
        -PassThru
    $processExitCode = $appProcess.ExitCode
}
catch {
    $launchFailure = "Scheduled launcher could not start or wait for the app: $($_.Exception.GetType().Name)"
    Send-FallbackAlert -Reason $launchFailure
    throw
}

"[{0}] Scheduled trading app stopped with exit code {1}." -f (
    Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
), $processExitCode | Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8

# Exit code 10 is reserved for a handled runner failure with confirmed Telegram
# delivery. Every other non-zero code receives a final outer-process fallback;
# this covers Python's generic unhandled-exception code 1 without duplicating a
# runner alert that was actually delivered.
if ($processExitCode -ne 0 -and $processExitCode -ne 10) {
    Send-FallbackAlert `
        -Reason "App process exited abnormally with code $processExitCode"
}

exit $processExitCode
