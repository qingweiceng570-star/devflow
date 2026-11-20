# Shared process identity checks. A reused PID never identifies our old service.
function Get-RecordedLocalProcess($Record) {
    $expectedNames = @{ api = 'python'; worker = 'python'; web = 'node' }
    if (-not $expectedNames.ContainsKey([string]$Record.name) -or [int]$Record.pid -le 0) { throw 'Invalid local process record.' }
    $candidate = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    if ($null -eq $candidate) { return $null }
    $expectedStart = if ($Record.started -is [datetime]) { $Record.started.ToUniversalTime() } else { [datetime]::Parse([string]$Record.started, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() }
    if ([Math]::Abs(($candidate.StartTime.ToUniversalTime() - $expectedStart).TotalMilliseconds) -gt 100) { return $null }
    if ($candidate.ProcessName -ne $expectedNames[[string]$Record.name]) { return $null }
    return $candidate
}

function Stop-RecordedLocalProcesses($Records) {
    foreach ($record in $Records) {
        $candidate = Get-RecordedLocalProcess $record
        if ($null -ne $candidate) {
            & taskkill.exe /PID $candidate.Id /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and (Get-RecordedLocalProcess $record)) { throw "Could not stop recorded service: $($record.name)." }
        }
    }
}

function Test-LocalPort([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait(500)) { return $false }
        return $client.Connected
    } catch { return $false } finally { $client.Dispose() }
}

function Resolve-LocalCommand([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $searchPath = @([Environment]::GetEnvironmentVariable('Path', 'User'), [Environment]::GetEnvironmentVariable('Path', 'Machine')) -join ';'
    foreach ($directory in ($searchPath -split ';')) {
        if (-not $directory) { continue }
        $directory = [Environment]::ExpandEnvironmentVariables($directory)
        foreach ($suffix in @('.exe', '.cmd', '.ps1')) {
            $candidate = Join-Path $directory ($Name + $suffix)
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    return $null
}
