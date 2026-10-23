$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
}
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Install Rust with rustup first. The vendored telemetry parser is built from Rust source."
}

uv sync --frozen --python 3.11
if ($LASTEXITCODE -ne 0) { throw "uv dependency installation failed." }
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
