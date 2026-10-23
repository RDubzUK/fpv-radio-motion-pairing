param([switch]$Check)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'windows_prerequisites.ps1')
Assert-FpvWindowsPrerequisites
if ($Check) { return }

uv sync --frozen --python 3.11
if ($LASTEXITCODE -ne 0) { throw "uv dependency installation failed. For telemetry-parser/link.exe errors, see README.md: Windows source setup. Check the MSVC C++ workload and Windows SDK in Visual Studio Installer." }
uv pip install --python .venv\Scripts\python.exe "pyinstaller==6.22.3"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }

.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm tools\windows.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$app = Join-Path $PWD "dist\FPV Matcher"
Copy-Item packaging\windows\README.txt $app
Copy-Item packaging\windows\Start-FPV-Matcher.bat $app
Copy-Item packaging\windows\Start-FPV-Matcher-LAN.bat $app

$zip = Join-Path $PWD "dist\FPV-Matcher-Windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $app "*") -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Portable test package created: $zip"
