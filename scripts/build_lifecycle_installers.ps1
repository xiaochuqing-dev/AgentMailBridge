[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OldSourceDir,
    [Parameter(Mandatory = $true)]
    [string]$NewSourceDir,
    [Parameter(Mandatory = $true)]
    [string]$OutputDir,
    [string]$OldVersion = "1.7.0",
    [string]$NewVersion = "1.7.1"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OldSourceDir = (Resolve-Path -LiteralPath $OldSourceDir).Path
$NewSourceDir = (Resolve-Path -LiteralPath $NewSourceDir).Path
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)

foreach ($source in @($OldSourceDir, $NewSourceDir)) {
    foreach ($name in @("AgentMailBridge.exe", "AgentMailBridgeMCP.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $source $name) -PathType Leaf)) {
            throw "Lifecycle installer source is incomplete: $source"
        }
    }
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$Iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    $candidate = Join-Path ${env:LOCALAPPDATA} "Programs\Inno Setup 6\ISCC.exe"
    if (Test-Path -LiteralPath $candidate) { $Iscc = $candidate }
}
if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) was not found." }

$InstallerScript = Join-Path $Root "packaging\windows\AgentMailBridge.iss"
$LifecycleGuid = (New-Guid).Guid.ToUpperInvariant()
$LifecycleAppId = "{{$LifecycleGuid}"
$LifecycleName = "AgentMailBridge Lifecycle Validation"

function Build-LifecycleInstaller {
    param(
        [string]$Version,
        [string]$Source,
        [string]$BaseName
    )
    & $Iscc `
        "/DMyAppVersion=$Version" `
        "/DSourceDir=$Source" `
        "/DOutputDir=$OutputDir" `
        "/DMyAppName=$LifecycleName" `
        "/DMyAppId=$LifecycleAppId" `
        "/DMyOutputBaseFilename=$BaseName" `
        $InstallerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Lifecycle installer build failed for $Version."
    }
}

$OldBaseName = "AgentMailBridge-$OldVersion-Lifecycle-Setup"
$NewBaseName = "AgentMailBridge-$NewVersion-Lifecycle-Setup"
Build-LifecycleInstaller -Version $OldVersion -Source $OldSourceDir -BaseName $OldBaseName
Build-LifecycleInstaller -Version $NewVersion -Source $NewSourceDir -BaseName $NewBaseName

$Result = [ordered]@{
    old_installer = (Join-Path $OutputDir "$OldBaseName.exe")
    new_installer = (Join-Path $OutputDir "$NewBaseName.exe")
    isolated_app_name = $LifecycleName
    app_id_randomized = $true
}
$Result | ConvertTo-Json
