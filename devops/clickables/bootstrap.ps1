param(
    [ValidateSet('setup','monitor','start','stop')][string]$Action = 'setup',
    [switch]$InstallOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$runtimeRoot = Join-Path $projectRoot 'Baymax-main'
$environmentPython = Join-Path $runtimeRoot '.venv\Scripts\python.exe'
$uvDirectory = Join-Path $projectRoot '.devtools'
$uvExecutable = Join-Path $uvDirectory 'uv.exe'
try {
    if ($Action -eq 'stop' -and -not (Test-Path -LiteralPath $environmentPython)) {
        Write-Host 'Ember has not been installed here; nothing to stop.'
        exit 0
    }
    if (-not (Test-Path -LiteralPath $uvExecutable)) {
        Write-Host 'First-time setup: downloading the uv installer from astral.sh. Please wait...'
        New-Item -ItemType Directory -Force -Path $uvDirectory | Out-Null
        $installer = Join-Path $uvDirectory 'uv-install.ps1'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer
        $env:UV_UNMANAGED_INSTALL = $uvDirectory
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $uvExecutable)) { throw 'Could not install uv. Check internet access and retry Setup.' }
    }
    $selectedPython = $environmentPython
    $usable = $false
    if (Test-Path -LiteralPath $environmentPython) {
        & $environmentPython -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)' 2>$null
        $usable = $LASTEXITCODE -eq 0
    }
    if (-not $usable) {
        Write-Host 'Preparing a private Python 3.12 runtime; your system Python is unchanged...'
        & $uvExecutable python install 3.12
        if ($LASTEXITCODE -ne 0) { throw 'Python download failed. Check internet access and retry Setup.' }
        $selectedPython = (& $uvExecutable python find --managed-python 3.12 | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0) { throw 'Could not locate Python 3.12.' }
    }
    $bootstrapArguments = @((Join-Path $PSScriptRoot 'bootstrap.py'), $Action, '--uv', $uvExecutable)
    if ($InstallOnly) { $bootstrapArguments += '--install-only' }
    & $selectedPython @bootstrapArguments
    exit $LASTEXITCODE
} catch {
    Write-Host ('Ember setup stopped: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Keep this window open or share the error with your lead. Never share API keys.'
    exit 1
}
