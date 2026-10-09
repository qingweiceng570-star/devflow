$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runRoot = Join-Path $projectRoot 'var'
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
$pidFile = Join-Path $runRoot 'local-processes.json'
if (Test-Path -LiteralPath $pidFile) { throw 'Local process record exists. Run scripts/stop_local.ps1 before starting again.' }
$backendPython = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
$nextCli = Join-Path $projectRoot 'frontend\node_modules\next\dist\bin\next'
$nodeRuntime = (Get-Command node -ErrorAction Stop).Source
$records = @()
try {
    $apiProcess = Start-Process -FilePath $backendPython -ArgumentList @('-m','uvicorn','app.main:app_factory','--factory','--host','127.0.0.1','--port','8100') -WorkingDirectory (Join-Path $projectRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot 'api.stdout.log') -RedirectStandardError (Join-Path $runRoot 'api.stderr.log') -PassThru
    $records += @{ name='api'; pid=$apiProcess.Id; started=$apiProcess.StartTime.ToUniversalTime().ToString('o') }
    $workerProcess = Start-Process -FilePath $backendPython -ArgumentList @('-m','app.worker') -WorkingDirectory (Join-Path $projectRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot 'worker.stdout.log') -RedirectStandardError (Join-Path $runRoot 'worker.stderr.log') -PassThru
    $records += @{ name='worker'; pid=$workerProcess.Id; started=$workerProcess.StartTime.ToUniversalTime().ToString('o') }
    $webProcess = Start-Process -FilePath $nodeRuntime -ArgumentList @($nextCli,'start','--hostname','127.0.0.1','--port','3100') -WorkingDirectory (Join-Path $projectRoot 'frontend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot 'web.stdout.log') -RedirectStandardError (Join-Path $runRoot 'web.stderr.log') -PassThru
    $records += @{ name='web'; pid=$webProcess.Id; started=$webProcess.StartTime.ToUniversalTime().ToString('o') }
} finally {
    ConvertTo-Json -InputObject @($records) | Set-Content -LiteralPath $pidFile -Encoding utf8
}
foreach ($healthUrl in @('http://127.0.0.1:8100/api/v1/health/ready', 'http://127.0.0.1:3100/login')) {
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try { Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw "Service did not become ready: $healthUrl. Check var/*.stderr.log and stop_local.ps1." }
}
Write-Output 'Started local API (8100), Worker and Web (3100). Logs are in var/. Run stop_local.ps1 to stop these processes.'
