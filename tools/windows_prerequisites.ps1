# Shared preflight for source installs and portable-package builds (Windows x64).
function Assert-FpvWindowsPrerequisites {
    param([switch]$RequireMediaTools)

    $Problems = New-Object 'System.Collections.Generic.List[string]'

    # rustup updates PATH for new terminals. Recover its normal installation in
    # an already-open terminal without changing the user's persistent PATH.
    $CargoRoots = @($env:CARGO_HOME)
    if ($env:USERPROFILE) { $CargoRoots += Join-Path $env:USERPROFILE '.cargo' }
    foreach ($CargoRoot in $CargoRoots) {
        if (-not $CargoRoot) { continue }
        $CargoBin = Join-Path $CargoRoot 'bin'
        if ((Test-Path (Join-Path $CargoBin 'cargo.exe')) -and
            (-not (Get-Command cargo -CommandType Application -ErrorAction SilentlyContinue) -or
             -not (Get-Command rustc -CommandType Application -ErrorAction SilentlyContinue))) {
            $env:PATH = "$CargoBin;$env:PATH"
        }
    }

    if (-not (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)) {
        $Problems.Add('Install uv: https://docs.astral.sh/uv/getting-started/installation/')
    }
    foreach ($RustTool in @('cargo', 'rustc')) {
        $RustCommand = Get-Command $RustTool -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $RustCommand) {
            $Problems.Add("Missing $RustTool. Install Rust with rustup: https://rust-lang.org/tools/install/")
            continue
        }
        try {
            $RustVersion = & $RustCommand.Source --version 2>&1
            if ($LASTEXITCODE -ne 0) { throw 'Rust toolchain is unavailable.' }
            $RustVersionText = ($RustVersion | Out-String).Trim()
            Write-Host $RustVersionText
        } catch {
            $Problems.Add("Cannot run $RustTool. Open a new terminal after installing Rust; if rustup has no default toolchain, run: rustup default stable")
        }
    }

    # Rust can discover MSVC without cl/link being on the current shell's PATH.
    $Linkers = @()
    foreach ($ProgramRoot in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $ProgramRoot) { continue }
        $VsWhere = Join-Path $ProgramRoot 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (Test-Path $VsWhere) {
            $Linkers += & $VsWhere -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find 'VC\Tools\MSVC\**\bin\Hostx64\x64\link.exe'
        }
    }
    if ($env:VCToolsInstallDir) {
        $DeveloperLinker = Join-Path $env:VCToolsInstallDir 'bin\Hostx64\x64\link.exe'
        if (Test-Path $DeveloperLinker) { $Linkers += $DeveloperLinker }
    }
    if (-not ($Linkers | Where-Object { $_ -and (Test-Path $_) })) {
        $Problems.Add('MSVC x64 build tools were not detected. Install Visual Studio Build Tools, select "Desktop development with C++", and include the MSVC x64/x86 tools: https://visualstudio.microsoft.com/visual-cpp-build-tools/')
    }

    $SdkRoots = @()
    $InstalledKits = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -ErrorAction SilentlyContinue
    if ($InstalledKits.KitsRoot10) { $SdkRoots += $InstalledKits.KitsRoot10 }
    if (${env:ProgramFiles(x86)}) { $SdkRoots += Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10' }
    $HasSdk = $false
    foreach ($SdkRoot in $SdkRoots) {
        $SdkVersions = Get-ChildItem -LiteralPath (Join-Path $SdkRoot 'Lib') -Directory -ErrorAction SilentlyContinue
        foreach ($SdkVersion in $SdkVersions) {
            if ((Test-Path (Join-Path $SdkVersion.FullName 'um\x64\kernel32.lib')) -and
                (Test-Path (Join-Path $SdkVersion.FullName 'ucrt\x64\ucrt.lib')) -and
                (Test-Path (Join-Path $SdkRoot "Include\$($SdkVersion.Name)\um\Windows.h"))) {
                $HasSdk = $true
            }
        }
    }
    if (-not $HasSdk) {
        $Problems.Add('Windows SDK x64 headers/libraries were not detected. In Visual Studio Installer, modify the C++ workload and include a Windows 10 or Windows 11 SDK.')
    }

    if ($RequireMediaTools) {
        foreach ($MediaTool in @('ffmpeg', 'ffprobe')) {
            if (-not (Get-Command $MediaTool -CommandType Application -ErrorAction SilentlyContinue)) {
                $Problems.Add("Missing $MediaTool on PATH. Install FFmpeg and include its bin folder: https://ffmpeg.org/download.html#build-windows")
            }
        }
    }
    if ($Problems.Count -gt 0) {
        throw ("Windows source prerequisites are incomplete:`n - " + ($Problems -join "`n - ") +
            "`nRestart PowerShell (and VS Code if using its terminal) after installing tools." +
            "`nSee README.md: Windows source setup. The portable ZIP needs no Rust or C++ build tools.")
    }
    Write-Host 'Windows source prerequisites found: uv, Rust/Cargo, MSVC x64 and Windows SDK.'
}
