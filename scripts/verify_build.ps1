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

$DistFiles = @(Get-ChildItem -LiteralPath $DistPath -Recurse -File)
$forbiddenNames = @('.env', 'credentials.json', 'token.json', 'agent_mail_bridge.db')
$leaks = $DistFiles | Where-Object {
    $forbiddenNames -contains $_.Name.ToLowerInvariant() -or
    $_.FullName -match '[\\/]secrets[\\/]'
}
if ($leaks) { throw "Forbidden files found in build: $($leaks.FullName -join ', ')" }

$DiscoveryDocuments = @($DistFiles | Where-Object {
    $_.FullName -match '[\\/]googleapiclient[\\/]discovery_cache[\\/]documents[\\/][^\\/]+\.json$'
})
if ($DiscoveryDocuments.Count -ne 1 -or $DiscoveryDocuments[0].Name -ne 'gmail.v1.json') {
    throw "Packaged Google discovery data must contain only gmail.v1.json: $($DiscoveryDocuments.FullName -join ', ')"
}

if (-not $SkipSelfTests) {
    $GuiSelfTest = Start-Process -FilePath $GuiExe -ArgumentList "--packaged-self-test" -Wait -PassThru -WindowStyle Hidden
    if ($GuiSelfTest.ExitCode -ne 0) { throw "GUI packaged self-test failed: $($GuiSelfTest.ExitCode)" }
    & python (Join-Path $PSScriptRoot "packaged_smoke.py") $McpExe
    if ($LASTEXITCODE -ne 0) { throw "MCP packaged smoke failed: $LASTEXITCODE" }
}

Write-Host "Build verification PASS: $DistPath"
