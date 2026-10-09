$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create the environment first: uv sync --frozen' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web/dist/index.html'))) { throw 'Build the interface first: cd web; pnpm run build' }
Push-Location $projectRoot
try { & $pythonPath -m zarya; exit $LASTEXITCODE } finally { Pop-Location }
