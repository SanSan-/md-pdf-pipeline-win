#requires -Version 5.1
[CmdletBinding()]
param([string] $Python)
$ErrorActionPreference = 'Stop'

function Get-BundlePath([string] $Relative, [string] $Type) {
    $path = Join-Path $PSScriptRoot $Relative
    if (-not (Test-Path -LiteralPath $path -PathType $Type)) {
        throw "Missing offline bundle item: $path. Extract the complete offline ZIP."
    }
    return $path
}

function Update-ProcessPath {
    $env:Path = @($env:Path, [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine')) -join ';'
}

function Get-Applications([string] $Name, [string[]] $Extra) {
    $found = @(Get-Command $Name -All -CommandType Application -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Source })
    @($found + $Extra) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -Unique
}

function Test-Python313([string] $File) {
    if (-not $File -or $File -match '\\Microsoft\\WindowsApps\\python(?:3)?\.exe$' -or
        -not (Test-Path -LiteralPath $File -PathType Leaf)) { return $false }
    $probe = "import sys, sysconfig; sys.exit(0 if sys.implementation.name == 'cpython' and sys.version_info[:2] == (3,13) and sysconfig.get_platform() == 'win-amd64' and not sysconfig.get_config_var('Py_GIL_DISABLED') else 1)"
    try {
        & $File -I -c $probe 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python313 {
    $extra = @($privatePython, "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:ProgramFiles\Python313\python.exe")
    foreach ($key in @('HKCU:\SOFTWARE\Python\PythonCore\3.13\InstallPath',
                       'HKLM:\SOFTWARE\Python\PythonCore\3.13\InstallPath')) {
        if (Test-Path -LiteralPath $key) {
            $entry = Get-Item -LiteralPath $key
            if ($entry.GetValue('ExecutablePath')) { $extra += $entry.GetValue('ExecutablePath') }
            if ($entry.GetValue('')) { $extra += Join-Path $entry.GetValue('') 'python.exe' }
        }
    }
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        try {
            $listed = @(& $launcher.Source -0p 2>$null)
            foreach ($line in $listed) {
                if ($line -match '^\s*-\S+\s+\*?\s*([A-Za-z]:\\.*?\.exe)\s*$') { $extra += $Matches[1] }
            }
        } catch { Write-Verbose "Could not list interpreters with $($launcher.Source)" }
    }
    foreach ($exe in (Get-Applications 'python.exe' $extra)) {
        if (Test-Python313 $exe) { return $exe }
    }
}

function Find-Pandoc {
    $extra = @("$env:LOCALAPPDATA\Pandoc\pandoc.exe", "$env:ProgramFiles\Pandoc\pandoc.exe")
    foreach ($exe in (Get-Applications 'pandoc.exe' $extra)) {
        try {
            $lines = @(& $exe --version 2>$null)
            if ($LASTEXITCODE -eq 0 -and $lines -and $lines[0] -match '^pandoc(?:\.exe)?\s+(\d+(?:\.\d+){1,3})' -and
                [version]$Matches[1] -ge [version]'3.8.2') { return $exe }
        } catch { Write-Verbose "Could not run $exe" }
    }
}

function Find-LuaLaTeX {
    # A fresh MiKTeX can report an update notice on stderr and still exit successfully.
    $ErrorActionPreference = 'Continue'
    $extra = @("$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64\lualatex.exe",
        "$env:ProgramFiles\MiKTeX\miktex\bin\x64\lualatex.exe")
    foreach ($exe in (Get-Applications 'lualatex.exe' $extra)) {
        try {
            $lines = @(& $exe --version 2>$null)
            if ($LASTEXITCODE -eq 0 -and $lines -and ($lines -join ' ') -match '(MiKTeX|TeX Live)') {
                return @{ File = $exe; Distribution = $Matches[1] }
            }
        } catch { Write-Verbose "Could not run $exe" }
    }
}

function Invoke-Installer([string] $File, [string] $Arguments) {
    Write-Host "Installing: $File $Arguments"
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) { throw "Installer failed ($($process.ExitCode)): $File" }
    Update-ProcessPath
}

function Invoke-Checked([string] $File, [string[]] $NativeArgs) {
    Write-Host "Running: $File $($NativeArgs -join ' ')"
    & $File @NativeArgs
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $File $($NativeArgs -join ' ')" }
}

function Install-LocalPackages([string] $Engine) {
    $manager = Join-Path (Split-Path -Parent $Engine) 'miktex.exe'
    if (-not (Test-Path -LiteralPath $manager -PathType Leaf)) { throw "Missing MiKTeX package manager: $manager" }
    $manifest = Get-BundlePath 'resources\miktex-packages.txt' Leaf
    $ids = @(Get-Content -LiteralPath $manifest -Encoding UTF8 | ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith('#') })
    if (-not $ids -or @($ids | Where-Object { $_ -notmatch '^[a-z0-9][a-z0-9.+_-]*$' }).Count) {
        throw "Invalid MiKTeX package list: $manifest"
    }
    Write-Host "Reading installed package IDs: $manager"
    $rows = @(& $manager packages list '--template={id}|{isInstalled}')
    if ($LASTEXITCODE -ne 0) { throw "Cannot list installed packages: $manager ($LASTEXITCODE)" }
    $installed = @{}
    foreach ($row in $rows) {
        if ($row -match '^(.+)\|true$') { $installed[$Matches[1]] = $true }
    }
    $missing = @($ids | Where-Object { -not $installed.ContainsKey($_) })
    if (-not $missing) {
        Write-Host "All $($ids.Count) required MiKTeX packages are installed."
        return
    }
    $repo = Get-BundlePath 'offline\miktex-packages' Container
    Invoke-Checked $manager @('packages', 'update-package-database', "--repository=$repo")
    Invoke-Checked $manager (@('packages', 'install', "--repository=$repo") + $missing)
}

$setup = Get-BundlePath 'setup.ps1' Leaf
$wheels = Get-BundlePath 'wheels' Container
if (-not (Get-ChildItem -LiteralPath $wheels -Filter '*.whl' -File | Select-Object -First 1)) {
    throw "No offline Python wheels found: $wheels"
}
$privateDir = Join-Path $env:LOCALAPPDATA 'Programs\md-pdf-pipeline\Python313'
$privatePython = Join-Path $privateDir 'python.exe'
$venv = Join-Path $PSScriptRoot '.venv'
if (Test-Path -LiteralPath $venv) {
    $runtime = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Python313 $runtime)) {
        throw "Existing .venv is incompatible with the bundled CPython 3.13 x64 wheels. Extract the ZIP into a fresh directory or move $venv."
    }
} elseif ($Python) {
    $runtime = (Get-Item -LiteralPath $Python).FullName
    if (-not (Test-Python313 $runtime)) { throw "Bundled wheels require standard CPython 3.13 x64: $runtime" }
} else {
    $runtime = Find-Python313
}
if (-not $runtime) {
    $installer = Get-BundlePath 'offline\installers\python-3.13.17-amd64.exe' Leaf
    Invoke-Installer $installer ('/quiet InstallAllUsers=0 Include_launcher=0 AssociateFiles=0 Shortcuts=0 PrependPath=0 Include_pip=1 Include_test=0 TargetDir="' + $privateDir + '"')
    $runtime = Find-Python313
    if (-not $runtime) { throw "CPython 3.13 x64 could not be started after installing $installer." }
}
Write-Host "CPython 3.13 x64: $runtime"

$pandoc = Find-Pandoc
if (-not $pandoc) {
    $msi = Get-BundlePath 'offline\installers\pandoc-3.8.2-windows-x86_64.msi' Leaf
    Invoke-Installer (Join-Path $env:SystemRoot 'System32\msiexec.exe') ('/i "' + $msi + '" /qn /norestart')
    $pandoc = Find-Pandoc
    if (-not $pandoc) { throw "Pandoc 3.8.2+ could not be started after installing $msi." }
}
Write-Host "Pandoc: $pandoc"

$engine = Find-LuaLaTeX
if (-not $engine) {
    $basic = Get-BundlePath 'offline\miktex-basic' Container
    $installer = Get-BundlePath 'offline\installers\miktexsetup_standalone.exe' Leaf
    Invoke-Checked $installer @('--shared=no', '--modify-path=yes', "--local-package-repository=$basic", '--package-set=basic', 'install')
    Update-ProcessPath
    $engine = Find-LuaLaTeX
    if (-not $engine) { throw "LuaLaTeX could not be started after installing $installer." }
}
Write-Host "LuaLaTeX ($($engine.Distribution)): $($engine.File)"
if ($engine.Distribution -eq 'MiKTeX') { Install-LocalPackages $engine.File }
$env:Path = @((Split-Path -Parent $pandoc), (Split-Path -Parent $engine.File), $env:Path) -join ';'
Write-Host "Running: $setup -Python $runtime -WheelDir $wheels"
& $setup -Python $runtime -WheelDir $wheels
exit $LASTEXITCODE
