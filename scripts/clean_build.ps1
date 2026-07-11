[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
foreach ($name in @("build", "dist", "release")) {
    $target = [System.IO.Path]::GetFullPath((Join-Path $Root $name))
    if (-not $target.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean outside the workspace: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
Write-Host "Build directories cleaned: $Root"
