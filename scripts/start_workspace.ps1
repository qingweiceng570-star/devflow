param([switch]$Check, [switch]$Rebuild, [switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local_runtime.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$runRoot = Join-Path $projectRoot 'var'
$pidFile = Join-Path $runRoot 'local-processes.json'
$backendRoot = Join-Path $projectRoot 'backend'
$frontendRoot = Join-Path $projectRoot 'frontend'
$backendPython = Join-Path $backendRoot '.venv\Scripts\python.exe'
$nextCli = Join-Path $frontendRoot 'node_modules\next\dist\bin\next'
$buildId = Join-Path $frontendRoot '.next\BUILD_ID'
$envFile = Join-Path $projectRoot '.env'
$composeFile = Join-Path $projectRoot 'infra\compose.yaml'
$nodeRuntime = Resolve-LocalCommand 'node'
$dockerRuntime = Resolve-LocalCommand 'docker'
$pnpmRuntime = Resolve-LocalCommand 'pnpm'
$pythonRuntime = if (Test-Path -LiteralPath $backendPython) { $backendPython } else { Resolve-LocalCommand 'python' }

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Label) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE). See the output above." }
}
function Test-ServiceReady([string]$Url) {
    try { Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true }
    catch { return $false }
}

$missing = @()
foreach ($dependency in @(@('Node.js 22-24', $nodeRuntime), @('Docker Desktop', $dockerRuntime), @('pnpm 10.33.2', $pnpmRuntime), @('Python 3.12-3.13', $pythonRuntime))) {
    if (-not $dependency[1]) { $missing += $dependency[0] }
    Write-Output ("{0}: {1}" -f $dependency[0], $(if ($dependency[1]) { 'found' } else { 'missing' }))
}
Write-Output ("Configuration: {0}; frontend build: {1}" -f (Test-Path -LiteralPath $envFile), (Test-Path -LiteralPath $buildId))
$records = if (Test-Path -LiteralPath $pidFile) { @(Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json) } else { @() }
$running = @($records | Where-Object { $null -ne (Get-RecordedLocalProcess $_) })
Write-Output "Recorded services alive: $($running.Count)/3"
if ($Check) {
    if ($pythonRuntime -and (Test-Path -LiteralPath $envFile)) {
        & $pythonRuntime (Join-Path $PSScriptRoot 'local_database_port.py') --check
        if ($LASTEXITCODE -ne 0) { $missing += 'usable PostgreSQL port' }
    }
    foreach ($port in @(8100, 3100)) { Write-Output "Port ${port}: $(Test-LocalPort $port)" }
    if ($missing.Count) { exit 1 }
    exit 0
}
if ($missing.Count) { throw "Install or add to PATH: $($missing -join ', '). Then run start_local.ps1 again." }
if ($Rebuild -and $SkipBuild) { throw 'Use either -Rebuild or -SkipBuild.' }
$nodeVersion = (([string](& $nodeRuntime --version)).Trim().TrimStart('v').Split('.')[0])
if ([int]$nodeVersion -lt 22 -or [int]$nodeVersion -ge 25) { throw 'Node.js 22-24 is required.' }
& $pythonRuntime -c 'import sys; sys.exit(0 if (3,12) <= sys.version_info[:2] < (3,14) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or 3.13 is required.' }
$env:Path = @((Split-Path -Parent $nodeRuntime), (Split-Path -Parent $dockerRuntime), (Split-Path -Parent $pnpmRuntime), $env:Path) -join ';'
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
$startLock = $null
try {
    try { $startLock = [IO.File]::Open((Join-Path $runRoot 'local-start.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw 'Another local startup is already running.' }
    $records = if (Test-Path -LiteralPath $pidFile) { @(Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json) } else { @() }
    $running = @($records | Where-Object { $null -ne (Get-RecordedLocalProcess $_) })
    if ($running.Count -eq 3 -and -not $Rebuild -and (Test-ServiceReady 'http://127.0.0.1:8100/api/v1/health/ready') -and (Test-ServiceReady 'http://127.0.0.1:3100/login')) {
        Write-Output 'Already running: http://localhost:3100'
        return
    }
    if ($records.Count) {
        Stop-RecordedLocalProcesses $records
        Remove-Item -LiteralPath $pidFile
        Write-Output 'Recovered old process records.'
    }
    foreach ($port in @(8100, 3100)) {
        if (Test-LocalPort $port) { throw "Port $port is occupied by an unrecorded process. Stop the conflicting service first." }
    }
    Push-Location $projectRoot
    try {
        if (-not (Test-Path -LiteralPath $envFile)) { Invoke-Checked $pythonRuntime @('scripts/init_env.py') 'Configuration initialization' }
        if (-not (Test-Path -LiteralPath $backendPython)) { Invoke-Checked $pythonRuntime @('-m', 'venv', 'backend/.venv') 'Virtual environment setup' }
        $uvRuntime = Join-Path $backendRoot '.venv\Scripts\uv.exe'
        if (-not (Test-Path -LiteralPath $uvRuntime)) { Invoke-Checked $backendPython @('-m', 'pip', 'install', 'uv==0.12.23') 'uv installation' }
        Write-Output 'Checking locked dependencies...'
        Invoke-Checked $uvRuntime @('sync', '--frozen', '--inexact', '--project', $backendRoot, '--python', $backendPython) 'Backend dependency sync'
        $installedLock = Join-Path $frontendRoot 'node_modules\.pnpm\lock.yaml'
        $sourceLock = Join-Path $frontendRoot 'pnpm-lock.yaml'
        $frontendInstalled = (Test-Path -LiteralPath $nextCli) -and (Test-Path -LiteralPath $installedLock)
        if ($frontendInstalled) { $frontendInstalled = (Get-FileHash -LiteralPath $sourceLock).Hash -eq (Get-FileHash -LiteralPath $installedLock).Hash }
        if (-not $frontendInstalled) {
            Invoke-Checked $pnpmRuntime @('--dir', $frontendRoot, 'install', '--frozen-lockfile', '--config.confirmModulesPurge=false', '--store-dir', (Join-Path $projectRoot '.pnpm-store')) 'Frontend dependency sync'
        }
        & $dockerRuntime info --format '{{.ServerVersion}}' *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Docker Engine is unavailable. Start Docker Desktop and retry.' }
        Invoke-Checked $backendPython @((Join-Path $PSScriptRoot 'local_database_port.py')) 'Local database port check'
        Write-Output 'Starting PostgreSQL and applying migrations...'
        Invoke-Checked $dockerRuntime @('compose', '--env-file', $envFile, '-f', $composeFile, 'up', '-d', '--wait', 'db') 'Database startup'
        Push-Location $backendRoot
        try {
            Invoke-Checked $backendPython @('-m', 'alembic', 'upgrade', 'head') 'Database migration'
            Invoke-Checked $backendPython @('-m', 'app.cli', 'create-admin', '--generate') 'Administrator setup'
        } finally { Pop-Location }
        $needsBuild = $Rebuild -or -not (Test-Path -LiteralPath $buildId)
        if (-not $needsBuild) {
            $builtAt = (Get-Item -LiteralPath $buildId).LastWriteTimeUtc
            $inputs = @(Get-ChildItem (Join-Path $frontendRoot 'src') -Recurse -File)
            $inputs += @(Get-ChildItem $frontendRoot -File | Where-Object { $_.Name -match '^(package\.json|pnpm-lock\.yaml|next\.config\..*|tsconfig\.json)$' })
            $needsBuild = @($inputs | Where-Object { $_.LastWriteTimeUtc -gt $builtAt }).Count -gt 0
        }
        if ($SkipBuild -and -not (Test-Path -LiteralPath $buildId)) { throw 'No frontend build exists; rerun without -SkipBuild.' }
        if ($needsBuild -and -not $SkipBuild) { Invoke-Checked $pnpmRuntime @('--dir', $frontendRoot, 'build') 'Frontend build' }
    } finally { Pop-Location }
    $records = @()
    try {
        foreach ($service in @(
            @{ name='api'; executable=$backendPython; arguments=@('-m','uvicorn','app.main:app_factory','--factory','--host','127.0.0.1','--port','8100'); directory=$backendRoot },
            @{ name='worker'; executable=$backendPython; arguments=@('-m','app.worker'); directory=$backendRoot },
            @{ name='web'; executable=$nodeRuntime; arguments=@(('"' + $nextCli + '"'),'start','--hostname','127.0.0.1','--port','3100'); directory=$frontendRoot }
        )) {
            $started = Start-Process -FilePath $service.executable -ArgumentList $service.arguments -WorkingDirectory $service.directory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot ($service.name + '.stdout.log')) -RedirectStandardError (Join-Path $runRoot ($service.name + '.stderr.log')) -PassThru
            $records += @{ name=$service.name; pid=$started.Id; started=$started.StartTime.ToUniversalTime().ToString('o') }
            ConvertTo-Json -InputObject @($records) | Set-Content -LiteralPath $pidFile -Encoding utf8
        }
        foreach ($healthUrl in @('http://127.0.0.1:8100/api/v1/health/ready', 'http://127.0.0.1:3100/login')) {
            $ready = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                if (Test-ServiceReady $healthUrl) { $ready = $true; break }
                if (@($records | Where-Object { $null -eq (Get-RecordedLocalProcess $_) }).Count) { break }
                Start-Sleep -Milliseconds 500
            }
            if (-not $ready) { throw "Service did not become ready: $healthUrl. Check var/*.stderr.log." }
        }
        if (@($records | Where-Object { $null -eq (Get-RecordedLocalProcess $_) }).Count) { throw 'A service exited during startup. Check var/*.stderr.log.' }
    } catch {
        Stop-RecordedLocalProcesses $records
        if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
        throw
    }
    Write-Output 'Ready: http://localhost:3100 . Initial credentials: var/initial-admin.json . Logs: var/*.stderr.log .'
} finally {
    if ($null -ne $startLock) { $startLock.Dispose() }
}
