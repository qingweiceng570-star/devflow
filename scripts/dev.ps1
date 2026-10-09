param([ValidateSet('api','worker','web','migrate','admin','test')][string]$Service)
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendPython = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
if ($Service -eq 'web') {
    Set-Location (Join-Path $projectRoot 'frontend')
    pnpm dev
    exit $LASTEXITCODE
}
Set-Location (Join-Path $projectRoot 'backend')
switch ($Service) {
    'api' { & $backendPython -m uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8100 }
    'worker' { & $backendPython -m app.worker }
    'migrate' { & $backendPython -m alembic upgrade head }
    'admin' { & $backendPython -m app.cli create-admin --generate }
    'test' { & $backendPython -m pytest -q }
}
exit $LASTEXITCODE
