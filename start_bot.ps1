$ErrorActionPreference = "Stop"

$projectDirectory = $PSScriptRoot
$python = Join-Path $projectDirectory "venv\Scripts\python.exe"
$envFile = Join-Path $projectDirectory ".env"
$logDirectory = Join-Path $projectDirectory "logs"
$launcherLog = Join-Path $logDirectory "launcher.log"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

try {
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Python virtual environment not found: $python"
    }
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw ".env configuration file not found: $envFile"
    }

    $runningBot = Get-CimInstance Win32_Process |
        Where-Object {
            $_.ExecutablePath -ieq $python -and
            $_.CommandLine -match '(?i)(^|[\\/\s"])bot\.py(["\s]|$)'
        } |
        Select-Object -First 1

    if ($runningBot) {
        Add-Content -LiteralPath $launcherLog -Value "$(Get-Date -Format s) Bot is already running (PID $($runningBot.ProcessId))."
        exit 0
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $logDirectory "bot-$timestamp.log"
    $stderrLog = Join-Path $logDirectory "bot-$timestamp-error.log"
    $process = Start-Process `
        -FilePath $python `
        -ArgumentList @("-u", "bot.py") `
        -WorkingDirectory $projectDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    Set-Content -LiteralPath (Join-Path $logDirectory "bot.pid") -Value $process.Id
    Add-Content -LiteralPath $launcherLog -Value "$(Get-Date -Format s) Bot started (PID $($process.Id)); stdout: $stdoutLog; stderr: $stderrLog."
}
catch {
    Add-Content -LiteralPath $launcherLog -Value "$(Get-Date -Format s) ERROR: $($_.Exception.Message)"
    throw
}
