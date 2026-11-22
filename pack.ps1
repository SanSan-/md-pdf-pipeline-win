#requires -Version 5.1
[CmdletBinding()]
param([string] $OfflineDir)

$ErrorActionPreference = 'Stop'
$dist = Join-Path $PSScriptRoot 'dist'
[IO.Directory]::CreateDirectory($dist) | Out-Null
$archiveName = 'md-pdf-pipeline.zip'
if ($OfflineDir) { $archiveName = 'md-pdf-pipeline-windows-x64-offline.zip' }
$archive = Join-Path $dist $archiveName
$stage = Join-Path $dist ('package-' + [Guid]::NewGuid().ToString('N') + '.zip')
$files = @('README.md', 'INSTALL-GUIDE.md', 'requirements.txt', 'build.ps1', 'setup.ps1', 'setup-offline.ps1',
    'pack.ps1', 'pipeline.py', 'book.py', 'images.py', 'tools/prepare_miktex.py',
    'resources/miktex-packages.txt', 'MIKTEX-PACKAGES.md',
    'examples/guide/index.md', 'examples/guide/figures/index.md',
    'examples/guide/figures/triangle.md', 'examples/guide/figures/assets/triangle.svg')
$rules = @(
    @{ Dir = 'resources'; Include = @('*.lua', '*.tex') },
    @{ Dir = 'tests'; Include = @('test_*.py') },
    @{ Dir = 'openspec'; Include = @('*.md', '*.yaml', '*.yml', '.gitkeep') }
)
foreach ($rule in $rules) {
    foreach ($pattern in $rule.Include) {
        $files += @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot $rule.Dir) -Recurse -File -Force -Filter $pattern |
            ForEach-Object { $_.FullName.Substring($PSScriptRoot.Length + 1).Replace('\', '/') })
    }
}
if ($OfflineDir) {
    $kit = (Get-Item -LiteralPath $OfflineDir -ErrorAction Stop).FullName
    $kitFiles = @()
    foreach ($name in @('offline/installers', 'offline/miktex-basic', 'offline/miktex-packages', 'wheels')) {
        $found = @(Get-ChildItem -LiteralPath (Join-Path $kit $name) -Recurse -File -ErrorAction Stop)
        if (-not $found.Count) { throw "Empty directory: $name" }
        $kitFiles += $found
    }
    foreach ($name in @('offline/installers/miktexsetup_standalone.exe', 'offline/miktex-basic/miktex-zzdb1-2.9.tar.lzma',
        'offline/miktex-packages/pr.ini')) {
        if (-not (Test-Path -LiteralPath (Join-Path $kit $name) -PathType Leaf)) { throw "Missing file: $name" }
    }
}
try {
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::Open($stage, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($name in $files) {
            $file = (Get-Item -LiteralPath (Join-Path $PSScriptRoot $name)).FullName
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file, $name) | Out-Null
        }
        foreach ($file in $kitFiles) {
            $name = $file.FullName.Substring($kit.TrimEnd('\').Length + 1).Replace('\', '/')
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName, $name,
                [IO.Compression.CompressionLevel]::NoCompression) | Out-Null
        }
    } finally {
        $zip.Dispose()
    }
    if ([IO.File]::Exists($archive)) {
        [IO.File]::Replace($stage, $archive, [NullString]::Value)
    } else {
        [IO.File]::Move($stage, $archive)
    }
    Write-Output $archive
} finally {
    if ([IO.File]::Exists($stage)) { [IO.File]::Delete($stage) }
}
