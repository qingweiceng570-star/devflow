param([switch]$Check, [switch]$Rebuild, [switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0
& (Join-Path $PSScriptRoot 'start_workspace.ps1') @PSBoundParameters
exit $LASTEXITCODE
