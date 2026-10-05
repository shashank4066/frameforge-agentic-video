[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location -LiteralPath $projectRoot

# Prefer the Windows Python launcher when available.
$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonLauncher -and -not $pythonCommand) {
    throw "Python 3.11 or newer is required. Install Python, then run this script again."
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    if ($pythonLauncher) {
        & $pythonLauncher.Source -3 -m venv (Join-Path $projectRoot ".venv")
    } else {
        & $pythonCommand.Source -m venv (Join-Path $projectRoot ".venv")
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}

& $venvPython -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
if ($LASTEXITCODE -ne 0) { throw "The environment requires Python 3.11 or newer." }

if (-not $SkipInstall) {
    & $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
}

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot ".env"))) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ".env.example") -Destination (Join-Path $projectRoot ".env")
}

# Resolve the same executable override used by the application, including .env.
$ffmpegCandidate = & $venvPython -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('FFMPEG_PATH') or 'ffmpeg')"
if ($LASTEXITCODE -ne 0) { throw "Could not read the application configuration." }
$ffmpegCommand = Get-Command $ffmpegCandidate.Trim() -ErrorAction SilentlyContinue
if (-not $ffmpegCommand) {
    throw "FFmpeg is required to render MP4 videos. Install FFmpeg and add it to PATH, or set FFMPEG_PATH in .env. Docker includes FFmpeg."
}
$ffmpegVersion = @(& $ffmpegCommand.Source -version)
if ($LASTEXITCODE -ne 0) { throw "FFmpeg could not run." }
Write-Host $ffmpegVersion[0]

Write-Host "FrameForge is starting at http://127.0.0.1:$Port"
Write-Host "Press Ctrl+C to stop. Your jobs and generated files remain in data/."
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
if ($LASTEXITCODE -ne 0) { throw "The application exited with an error." }
