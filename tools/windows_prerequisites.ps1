# Source setup for Windows x64. Installers run only through Initialize-FpvWindowsPrerequisites.
function Update-FpvWindowsToolPath {
    $PathValues = @($env:PATH,
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User'))
    if ($env:CARGO_HOME) { $PathValues += Join-Path $env:CARGO_HOME 'bin' }
    if ($env:USERPROFILE) {
        $PathValues += Join-Path $env:USERPROFILE '.cargo\bin'
        $PathValues += Join-Path $env:USERPROFILE '.local\bin'
    }
    if ($env:LOCALAPPDATA) {
        $PathValues += Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
        $PathValues += Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    }
    if ($env:ProgramFiles) { $PathValues += Join-Path $env:ProgramFiles 'WinGet\Links' }
    $SeenPaths = @{}
    $ToolPaths = New-Object 'System.Collections.Generic.List[string]'
    foreach ($PathValue in $PathValues) {
        foreach ($Entry in ($PathValue -split ';')) {
            if ([string]::IsNullOrWhiteSpace($Entry)) { continue }
            $ExpandedEntry = [Environment]::ExpandEnvironmentVariables($Entry.Trim().Trim('"'))
            if (-not $SeenPaths.ContainsKey($ExpandedEntry)) {
                $SeenPaths[$ExpandedEntry] = $true
                $ToolPaths.Add($ExpandedEntry)
            }
        }
    }
    # Preserve this process's custom paths; do not overwrite the persistent PATH.
    $env:PATH = $ToolPaths -join ';'
}

function Test-FpvWindowsCommand {
    param([string]$Name, [switch]$VersionCheck)
    $ToolCommand = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ToolCommand) { return $false }
    if (-not $VersionCheck) { return $true }
    try {
        & $ToolCommand.Source --version 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Get-FpvWindowsPrerequisites {
    param([switch]$RequireMediaTools)
    $Status = [ordered]@{
        UV = (Test-FpvWindowsCommand uv)
        Cargo = (Test-FpvWindowsCommand cargo -VersionCheck)
        Rustc = (Test-FpvWindowsCommand rustc -VersionCheck)
        MSVC = $false
        SDK = $false
        FFmpeg = (Test-FpvWindowsCommand ffmpeg)
        FFprobe = (Test-FpvWindowsCommand ffprobe)
    }
    # Rust discovers MSVC through Visual Studio even outside a developer shell.
    $VsWherePath = $null
    $Linkers = @()
    foreach ($ProgramRoot in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $ProgramRoot) { continue }
        $VsWhere = Join-Path $ProgramRoot 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (Test-Path $VsWhere) {
            $VsWherePath = $VsWhere
            $Linkers += & $VsWhere -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find 'VC\Tools\MSVC\**\bin\Hostx64\x64\link.exe'
        }
    }
    if ($env:VCToolsInstallDir) {
        $DeveloperLinker = Join-Path $env:VCToolsInstallDir 'bin\Hostx64\x64\link.exe'
        if (Test-Path $DeveloperLinker) { $Linkers += $DeveloperLinker }
    }
    $Status.MSVC = @($Linkers | Where-Object { $_ -and (Test-Path $_) }).Count -gt 0
    $SdkRoots = @($env:WindowsSdkDir)
    $InstalledKits = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -ErrorAction SilentlyContinue
    if ($InstalledKits.KitsRoot10) { $SdkRoots += $InstalledKits.KitsRoot10 }
    if (${env:ProgramFiles(x86)}) { $SdkRoots += Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10' }
    foreach ($SdkRoot in $SdkRoots) {
        if (-not $SdkRoot) { continue }
        foreach ($SdkVersion in (Get-ChildItem -LiteralPath (Join-Path $SdkRoot 'Lib') -Directory -ErrorAction SilentlyContinue)) {
            if ((Test-Path (Join-Path $SdkVersion.FullName 'um\x64\kernel32.lib')) -and
                (Test-Path (Join-Path $SdkVersion.FullName 'ucrt\x64\ucrt.lib')) -and
                (Test-Path (Join-Path $SdkRoot "Include\$($SdkVersion.Name)\um\Windows.h"))) {
                $Status.SDK = $true
            }
        }
    }
    $Required = @('UV', 'Cargo', 'Rustc', 'MSVC', 'SDK')
    if ($RequireMediaTools) { $Required += @('FFmpeg', 'FFprobe') }
    $Status['Missing'] = @($Required | Where-Object { -not $Status[$_] })
    $Status['VsWhere'] = $VsWherePath
    return [pscustomobject]$Status
}

function Assert-FpvWindowsPrerequisites {
    param([switch]$RequireMediaTools)
    Update-FpvWindowsToolPath
    $Status = Get-FpvWindowsPrerequisites -RequireMediaTools:$RequireMediaTools
    $HelpByTool = @{
        UV = 'uv: https://docs.astral.sh/uv/getting-started/installation/'
        Cargo = 'Cargo (Rust): https://rust-lang.org/tools/install/'
        Rustc = 'Rust compiler/default toolchain: https://rust-lang.org/tools/install/'
        MSVC = 'MSVC x64 compiler/linker: Visual Studio Build Tools, Desktop development with C++'
        SDK = 'Windows SDK: include a Windows 10 or Windows 11 SDK in the C++ workload'
        FFmpeg = 'FFmpeg on PATH: https://ffmpeg.org/download.html#build-windows'
        FFprobe = 'ffprobe on PATH: supplied with FFmpeg'
    }
    if ($Status.Missing.Count -gt 0) {
        $Problems = @($Status.Missing | ForEach-Object { $HelpByTool[$_] })
        throw ("Windows source prerequisites are incomplete:`n - " + ($Problems -join "`n - ") +
            "`nRun start.ps1 without -Check/-SkipInstall to install missing tools automatically." +
            "`nSee README.md: Windows source setup. Portable ZIPs need no Rust or C++ compiler.")
    }
    Write-Host 'Windows source prerequisites found: uv, Rust/Cargo, MSVC x64 and Windows SDK.'
    if ($RequireMediaTools) { Write-Host 'FFmpeg and ffprobe found.' }
}

function Assert-FpvSetupExitCode {
    param([long]$Code, [string]$Label)
    $ExitHex = '{0:X8}' -f ($Code -band 4294967295)
    # Native installer codes and WinGet's corresponding HRESULTs. Never reboot automatically.
    if ($ExitHex -in @('00000BC2', '00000669', '8A150109', '8A15010A', '8A15010B')) {
        throw "$Label requires a Windows restart. Restart the PC, then rerun the same command; completed installations will be reused."
    }
    if ($ExitHex -in @('00000000', '8A15002B', '8A150061', '8A15010D')) { return }
    throw "$Label failed (exit $Code / 0x$ExitHex). Read the installer output above. Allow any Windows permission prompt, then retry; see README.md: Windows source setup."
}

function Install-FpvWinGetPackage {
    param([string]$PackageId, [string]$InstallerOptions)
    $PackageArguments = @('install', '--id', $PackageId, '--exact', '--source', 'winget',
        '--no-upgrade', '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity')
    if ($InstallerOptions) { $PackageArguments += @('--override', $InstallerOptions) }
    else { $PackageArguments += '--silent' }
    Write-Host "Installing $PackageId. Allow the Windows administrator prompt if shown."
    & winget @PackageArguments | Out-Host
    $PackageExit = $LASTEXITCODE
    Assert-FpvSetupExitCode -Code $PackageExit -Label $PackageId
    Update-FpvWindowsToolPath
}

function Install-FpvWindowsBuildTools {
    param($Status)
    $BuildToolsPath = $null
    if ($Status.VsWhere) {
        $BuildToolsPath = & $Status.VsWhere -latest -version '[17.0,18.0)' -products Microsoft.VisualStudio.Product.BuildTools -property installationPath | Select-Object -First 1
    }
    if ($BuildToolsPath) {
        $VsInstaller = Join-Path (Split-Path -Parent $Status.VsWhere) 'setup.exe'
        if (Test-Path $VsInstaller) {
            # WinGet install skips an installed product, so modify an existing
            # Build Tools instance that is missing its C++ workload explicitly.
            Write-Host 'Adding the C++ workload to existing Visual Studio Build Tools. Close Visual Studio and allow the Windows permission prompt.'
            $ModifyArguments = 'modify --installPath "{0}" --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --norestart' -f $BuildToolsPath
            $SetupProcess = Start-Process -FilePath $VsInstaller -ArgumentList $ModifyArguments -Verb RunAs -Wait -PassThru
            Assert-FpvSetupExitCode -Code $SetupProcess.ExitCode -Label 'Visual Studio C++ workload'
            return
        }
    }
    Install-FpvWinGetPackage -PackageId 'Microsoft.VisualStudio.2022.BuildTools' -InstallerOptions '--wait --passive --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
}

function Initialize-FpvWindowsPrerequisites {
    param([switch]$RequireMediaTools, [switch]$Check, [switch]$SkipInstall)
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess) {
        throw 'Use these source setup scripts from 64-bit Windows PowerShell. On Linux, use start.sh.'
    }
    Update-FpvWindowsToolPath
    $Status = Get-FpvWindowsPrerequisites -RequireMediaTools:$RequireMediaTools
    if ($Check -or $SkipInstall -or $Status.Missing.Count -eq 0) {
        Assert-FpvWindowsPrerequisites -RequireMediaTools:$RequireMediaTools
        return
    }
    if (-not (Test-FpvWindowsCommand winget)) {
        throw 'Automatic setup needs WinGet. Install/update App Installer from https://apps.microsoft.com/detail/9nblggh4nns1 and rerun this command. Manual prerequisites are listed in README.md.'
    }
    Write-Host ('Installing missing Windows prerequisites: ' + ($Status.Missing -join ', '))
    Write-Host 'The first C++ toolchain download can be several GB. Package/source agreements are accepted for these prerequisites; Windows may request administrator permission.'
    if (-not $Status.UV) { Install-FpvWinGetPackage -PackageId 'astral-sh.uv' }
    if (-not (Test-FpvWindowsCommand uv)) { throw 'uv is still unavailable after installation. Restart PowerShell/VS Code and rerun the same command.' }
    # Install the native toolchain before Rust to avoid rustup's separate Visual Studio bootstrap.
    if (-not $Status.MSVC) { Install-FpvWindowsBuildTools -Status $Status }
    $Status = Get-FpvWindowsPrerequisites -RequireMediaTools:$RequireMediaTools
    if (-not $Status.SDK) { Install-FpvWinGetPackage -PackageId 'Microsoft.WindowsSDK.10.0.26100' }
    $Status = Get-FpvWindowsPrerequisites -RequireMediaTools:$RequireMediaTools
    if (-not $Status.MSVC -or -not $Status.SDK) {
        throw 'The C++ compiler/linker or Windows SDK is still unavailable after setup. Restart Windows if requested, then run start.ps1 -Check. Inspect Visual Studio Installer if the prerequisite is still missing.'
    }
    if (-not $Status.Cargo -or -not $Status.Rustc) {
        if (-not (Test-FpvWindowsCommand rustup)) { Install-FpvWinGetPackage -PackageId 'Rustlang.Rustup' }
        Update-FpvWindowsToolPath
        if (-not (Test-FpvWindowsCommand rustup)) { throw 'Rustup is still unavailable after installation. Restart PowerShell/VS Code and rerun the same command.' }
        if (-not (Test-FpvWindowsCommand cargo -VersionCheck) -or -not (Test-FpvWindowsCommand rustc -VersionCheck)) {
            $ActiveRustExit = 1
            try {
                & rustup show active-toolchain 2>&1 | Out-Null
                $ActiveRustExit = $LASTEXITCODE
            } catch { $ActiveRustExit = 1 }
            if ($ActiveRustExit -ne 0) {
                & rustup default stable-x86_64-pc-windows-msvc | Out-Host
            } else {
                # Preserve an existing active toolchain; add missing components.
                & rustup component add cargo rustc | Out-Host
            }
            Assert-FpvSetupExitCode -Code $LASTEXITCODE -Label 'Rust toolchain setup'
        }
    }
    Update-FpvWindowsToolPath
    if ($RequireMediaTools -and (-not (Test-FpvWindowsCommand ffmpeg) -or -not (Test-FpvWindowsCommand ffprobe))) {
        Install-FpvWinGetPackage -PackageId 'Gyan.FFmpeg'
    }
    Assert-FpvWindowsPrerequisites -RequireMediaTools:$RequireMediaTools
}
