$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local_runtime.ps1')
$script:sampleProcess = $null
$script:stopped = 0
function Get-Process { [CmdletBinding()] param([int]$Id) return $script:sampleProcess }
function taskkill.exe { $script:stopped++; $global:LASTEXITCODE = 0 }
function Assert-True($Value, [string]$Message) { if (-not $Value) { throw $Message } }
$record = @{ name='api'; pid=123; started='2026-10-26T00:00:00.0000000Z' }
Assert-True ($null -eq (Get-RecordedLocalProcess $record)) 'Missing processes must be stale.'
$script:sampleProcess = [pscustomobject]@{ Id=123; ProcessName='python'; StartTime=[datetime]'2026-10-26T01:00:00Z' }
Stop-RecordedLocalProcesses @($record)
Assert-True ($script:stopped -eq 0) 'A reused PID must never be terminated.'
$script:sampleProcess.StartTime = [datetime]'2026-10-26T00:00:00Z'
$script:sampleProcess.ProcessName = 'unrelated'
Stop-RecordedLocalProcesses @($record)
Assert-True ($script:stopped -eq 0) 'An unrelated process must never be terminated.'
$script:sampleProcess.ProcessName = 'python'
Stop-RecordedLocalProcesses @($record)
Assert-True ($script:stopped -eq 1) 'A matching service must be stopped.'
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
try {
    $listener.Start()
    $testPort = $listener.LocalEndpoint.Port
    Assert-True (Test-LocalPort $testPort) 'Occupied ports must be detected.'
} finally { $listener.Stop() }
Assert-True (-not (Test-LocalPort $testPort)) 'Closed ports must be available.'
Write-Output 'Passed: stale records, PID reuse, process identity, owned process cleanup, port detection.'
