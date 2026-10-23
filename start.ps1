param([switch]$Lan, [int]$Port = 8767)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
foreach ($Tool in @("uv", "ffmpeg", "ffprobe", "cargo")) {
    if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) {
        throw "Missing $Tool. See README.md. Rust/cargo is needed to build the source telemetry parser; portable packages do not need it."
    }
}
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
uv sync --frozen --python 3.11
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
$BindAddress = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
uv run --frozen fpv-match-web --host $BindAddress --port $Port
if ($LASTEXITCODE -ne 0) { throw "Flight Matcher stopped with an error. Run uv run --frozen fpv-match-web --check for diagnostics." }
