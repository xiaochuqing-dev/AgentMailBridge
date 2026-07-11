[CmdletBinding()]
param(
    [string]$DistPath = "",
    [switch]$SkipSelfTests
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $DistPath) { $DistPath = Join-Path $Root "dist\AgentMailBridge" }
$DistPath = [System.IO.Path]::GetFullPath($DistPath)

$GuiExe = Join-Path $DistPath "AgentMailBridge.exe"
$McpExe = Join-Path $DistPath "AgentMailBridgeMCP.exe"
foreach ($file in @($GuiExe, $McpExe)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing build artifact: $file" }
}

$forbiddenNames = @('.env', 'credentials.json', 'token.json', 'agent_mail_bridge.db')
$leaks = Get-ChildItem -LiteralPath $DistPath -Recurse -File | Where-Object {
    $forbiddenNames -contains $_.Name.ToLowerInvariant() -or
    $_.FullName -match '[\\/]secrets[\\/]'
}
if ($leaks) { throw "Forbidden files found in build: $($leaks.FullName -join ', ')" }

if (-not $SkipSelfTests) {
    & $GuiExe --packaged-self-test
    if ($LASTEXITCODE -ne 0) { throw "GUI packaged self-test failed: $LASTEXITCODE" }
    & python (Join-Path $PSScriptRoot "packaged_smoke.py") $McpExe
    if ($LASTEXITCODE -ne 0) { throw "MCP packaged smoke failed: $LASTEXITCODE" }
}

Write-Host "Build verification PASS: $DistPath"
