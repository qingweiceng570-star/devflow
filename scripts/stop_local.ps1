$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot 'var\local-processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No local process record.'; exit 0 }
$records = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
foreach ($record in $records) {
    $localProcess = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($null -ne $localProcess) {
        $expectedStart = if ($record.started -is [datetime]) { $record.started.ToUniversalTime() } else { [datetime]::Parse($record.started, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() }
        $actualStart = $localProcess.StartTime.ToUniversalTime()
        if ([Math]::Abs(($actualStart - $expectedStart).TotalMilliseconds) -gt 100) { throw "PID $($record.pid) has been reused; refusing to stop it." }
        if ($localProcess.ProcessName -notin @('python','node')) { throw 'Unexpected process type; refusing to stop it.' }
        # The Windows virtual-environment launcher can own a child Python process.
        & taskkill.exe /PID $record.pid /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not stop recorded process $($record.pid)." }
    }
}
Remove-Item -LiteralPath $pidFile
Write-Output 'Stopped the recorded local processes. Database and data were kept.'
