Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runnerPath = Join-Path $projectRoot "scripts\run_scheduled_app.py"
$logDirectory = Join-Path $projectRoot "logs"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project Python was not found at $pythonPath"
}
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Scheduled Python runner was not found at $runnerPath"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$runId = Get-Date -Format "yyyy-MM-dd-HHmmss"
$lifecycleLog = Join-Path $logDirectory "scheduled-app-lifecycle.log"
$standardOutputLog = Join-Path $logDirectory "scheduled-app-$runId.stdout.log"
$standardErrorLog = Join-Path $logDirectory "scheduled-app-$runId.stderr.log"

Set-Location -LiteralPath $projectRoot
"[{0}] Scheduled trading app starting." -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss K") |
    Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8

try {
    $runningHealth = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8000/health" `
        -TimeoutSec 5
    $tokenFile = Join-Path $projectRoot "access_token.txt"
    $storedTokenMetadata = Get-Content -LiteralPath $tokenFile -Raw | ConvertFrom-Json
    $today = Get-Date -Format "yyyy-MM-dd"
    if (
        $runningHealth.status -eq "ok" -and
        $storedTokenMetadata.trading_date -eq $today
    ) {
        "[{0}] A healthy app with today's Kite token is already running; duplicate start skipped." -f (
            Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
        ) | Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8
        exit 0
    }
}
catch {
    # No healthy local app answered, so continue with a normal scheduled start.
}

$env:AUTOMATION_STOP_AFTER_AFTER_MARKET_COMPLETE = "true"
$env:SCHEDULED_RUN_EXIT_AFTER_COMPLETE = "true"

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

"[{0}] Scheduled trading app stopped with exit code {1}." -f (
    Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
), $processExitCode | Out-File -LiteralPath $lifecycleLog -Append -Encoding utf8

exit $processExitCode
