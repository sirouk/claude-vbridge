# Per-user launcher. No Windows service, elevation, password storage, or policy bypass.
[CmdletBinding()]
param(
    [ValidateSet('install','start','restart','status','check','permissions','settings','connector','logs','stop','help')]
    [string]$Action = 'start',
    [string]$PublicUrl,
    [switch]$ReplaceTask
)
$ErrorActionPreference = 'Stop'
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$Admin = Join-Path $PSScriptRoot 'scripts\bridge_admin.py'
$State = if ($env:VBRIDGE_HOME) { $env:VBRIDGE_HOME } else { Join-Path $HOME '.vbridge' }
if ($Action -eq 'help') {
    Write-Output 'VBridge — direct Claude connector to this Windows user session'
    Write-Output '.\windows-bridge.ps1 install -PublicUrl https://YOUR-HOSTNAME [-ReplaceTask]'
    Write-Output '.\windows-bridge.ps1 [start|restart|status|check|permissions|settings|connector|logs|stop|help]'
    Write-Output 'No argument = start. Run as your normal logged-in user, not as administrator.'
    exit 0
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Missing project environment. Run uv sync --locked first.'
}
if ($Action -eq 'logs') {
    Get-Content -LiteralPath (Join-Path $State 'server.log') -Tail 80 -Wait
    exit 0
}
$AdminArgs = @($Admin, $Action)
if ($Action -eq 'install') {
    if ($PublicUrl) { $AdminArgs += @('--public-url', $PublicUrl) }
    if ($ReplaceTask) { $AdminArgs += '--replace-task' }
}
& $Python @AdminArgs
exit $LASTEXITCODE
