#requires -Version 5.1
[CmdletBinding(DefaultParameterSetName = 'Build')]
param(
    [Parameter(Mandatory = $true, Position = 0, ParameterSetName = 'Build')]
    [string] $Source,
    [Parameter(Position = 1, ParameterSetName = 'Build')]
    [string] $Output,
    [Parameter(ParameterSetName = 'Build')]
    [string] $Title,
    [Parameter(ParameterSetName = 'Build')]
    [string] $CacheDir,
    [Parameter(ParameterSetName = 'Build')]
    [switch] $Offline,
    [Parameter(Mandatory = $true, ParameterSetName = 'Check')]
    [switch] $Check
)

$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'Python .venv: .\setup.ps1 (INSTALL-GUIDE.md)'
}
$cli = @('-B', '-X', 'utf8', (Join-Path $PSScriptRoot 'pipeline.py'))
if ($Check) {
    $cli += '--check'
} else {
    if ($Output) { $cli += @('--output', $Output) }
    if ($Title) { $cli += @('--title', $Title) }
    if ($CacheDir) { $cli += @('--cache-dir', $CacheDir) }
    if ($Offline) { $cli += '--offline' }
    $cli += @('--', $Source)
}
$originalPath = $env:Path
try {
    if (-not (Get-Command pandoc.exe -CommandType Application -ErrorAction SilentlyContinue) -or
        -not (Get-Command lualatex.exe -CommandType Application -ErrorAction SilentlyContinue)) {
        $env:Path = @($originalPath, [Environment]::GetEnvironmentVariable('Path', 'User'),
            [Environment]::GetEnvironmentVariable('Path', 'Machine')) -join ';'
    }
    & $pythonExe @cli
    $code = $LASTEXITCODE
} finally {
    $env:Path = $originalPath
}
exit $code
