[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Version = (& python -c "from agent_mail_bridge import __version__; print(__version__)" ).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) { throw "Unable to read product version." }

& (Join-Path $PSScriptRoot "clean_build.ps1")
$PreflightArgs = @((Join-Path $PSScriptRoot "full_suite_preflight.py"))
if ($SkipTests) { $PreflightArgs += "--skip-tests" }
& python @PreflightArgs
if ($LASTEXITCODE -ne 0) { throw "Full Suite Preflight failed." }
if (-not $SkipTests) {
    & python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Automated tests failed." }
}

$SpecPath = Join-Path $Root "packaging\windows\AgentMailBridge.spec"
& python -m PyInstaller --noconfirm --clean $SpecPath
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
& (Join-Path $PSScriptRoot "verify_build.ps1")

$Release = Join-Path $Root "release"
New-Item -ItemType Directory -Path $Release -Force | Out-Null
$Portable = Join-Path $Release "AgentMailBridge-$Version-Windows-x64.zip"
Compress-Archive -LiteralPath (Join-Path $Root "dist\AgentMailBridge") -DestinationPath $Portable -CompressionLevel Optimal

if (-not $SkipInstaller) {
    $Iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
    if (-not $Iscc) {
        $candidate = Join-Path ${env:LOCALAPPDATA} "Programs\Inno Setup 6\ISCC.exe"
        if (Test-Path -LiteralPath $candidate) { $Iscc = $candidate }
    }
    if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) was not found." }
    $InstallerScript = Join-Path $Root "packaging\windows\AgentMailBridge.iss"
    & $Iscc "/DMyAppVersion=$Version" $InstallerScript
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed." }
}

$Artifacts = Get-ChildItem -LiteralPath $Release -File | Where-Object { $_.Extension -in '.exe', '.zip' }
$ChecksumPath = Join-Path $Release "checksums.sha256"
$ChecksumLines = foreach ($artifact in $Artifacts) {
    $hash = (Get-FileHash -LiteralPath $artifact.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($artifact.Name)"
}
[System.IO.File]::WriteAllLines($ChecksumPath, $ChecksumLines, [System.Text.UTF8Encoding]::new($false))
& python (Join-Path $PSScriptRoot "secret_scan.py")
if ($LASTEXITCODE -ne 0) { throw "Secret exclusion scan failed." }
Write-Host "Windows build completed: $Release"
