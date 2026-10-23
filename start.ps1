param([switch]$Lan, [int]$Port = 8767, [switch]$Check, [switch]$SkipInstall)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot 'tools\windows_prerequisites.ps1')
Initialize-FpvWindowsPrerequisites -RequireMediaTools -Check:$Check -SkipInstall:$SkipInstall
if ($Check) { return }
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
uv sync --frozen --python 3.11
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. For telemetry-parser/link.exe errors, see README.md: Windows source setup. Check the MSVC C++ workload and Windows SDK in Visual Studio Installer." }
$BindAddress = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
uv run --frozen fpv-match-web --host $BindAddress --port $Port
if ($LASTEXITCODE -ne 0) { throw "Flight Matcher stopped with an error. Run uv run --frozen fpv-match-web --check for diagnostics." }
