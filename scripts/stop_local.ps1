$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local_runtime.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot 'var\local-processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No local process record.'; exit 0 }
$records = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
Stop-RecordedLocalProcesses $records
Remove-Item -LiteralPath $pidFile
Write-Output 'Stopped the recorded local processes. Database and data were kept.'
