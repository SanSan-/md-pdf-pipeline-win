#requires -Version 5.1
[CmdletBinding()]
param(
    [string] $Python,
    [switch] $InstallTools,
    [string] $WheelDir
)
$ErrorActionPreference = 'Stop'

function Test-Interpreter([string] $File, [string[]] $PrefixArgs) {
    if (-not $File) { return $false }
    try {
        & $File @PrefixArgs -I -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) and sys.maxsize > 2**32 else 1)' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Interpreter {
    if ($Python) {
        $exe = (Get-Item -LiteralPath $Python).FullName
        if (-not (Test-Interpreter $exe @())) { throw "Python 3.11+ x64: $exe" }
        return @{ File = $exe; Prefix = @() }
    }
    $py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        foreach ($selectVersion in @('-3.13', '-3')) {
            if (Test-Interpreter $py.Source @($selectVersion)) {
                return @{ File = $py.Source; Prefix = @($selectVersion) }
            }
        }
    }
    foreach ($exe in @(Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue)) {
        if ($exe.Source -match '\\Microsoft\\WindowsApps\\python.exe$') { continue }
        if (Test-Interpreter $exe.Source @()) { return @{ File = $exe.Source; Prefix = @() } }
    }
}

function Install-Tool([string] $Id) {
    & winget.exe install --id $Id --exact --source winget --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget: $Id ($LASTEXITCODE). INSTALL-GUIDE.md" }
    $env:Path = @($env:Path,
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine')) -join ';'
}

$venvDir = Join-Path $PSScriptRoot '.venv'
$venvExe = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvExe -PathType Leaf)) {
    $runtime = Find-Interpreter
    if (-not $runtime -and $InstallTools) {
        Install-Tool 'Python.Python.3.13'
        $runtime = Find-Interpreter
    }
    if (-not $runtime) { throw 'Python 3.11+ x64: setup.ps1 -InstallTools / -Python <python.exe>. INSTALL-GUIDE.md' }
    $file = $runtime.File
    $prefixArgs = $runtime.Prefix
    & $file @prefixArgs -X utf8 -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
if (-not (Test-Interpreter $venvExe @())) { throw '.venv: INSTALL-GUIDE.md' }
if ($InstallTools) {
    if (-not (Get-Command pandoc.exe -CommandType Application -ErrorAction SilentlyContinue)) {
        Install-Tool 'JohnMacFarlane.Pandoc'
    }
    if (-not (Get-Command lualatex.exe -CommandType Application -ErrorAction SilentlyContinue)) {
        Install-Tool 'MiKTeX.MiKTeX'
    }
}
$pipArgs = @('-X', 'utf8', '-m', 'pip', '--disable-pip-version-check', 'install', '--only-binary=:all:')
if ($WheelDir) {
    $pipArgs += @('--no-index', '--find-links', (Get-Item -LiteralPath $WheelDir).FullName)
}
& $venvExe @pipArgs -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venvExe -B -X utf8 (Join-Path $PSScriptRoot 'pipeline.py') --check
exit $LASTEXITCODE
