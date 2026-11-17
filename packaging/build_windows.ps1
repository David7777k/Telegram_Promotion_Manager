param(
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonExe = (Get-Command $Python -CommandType Application).Source
Push-Location $root
try {
    $name = "TelegramPromotionStudio"
    $dist = Join-Path $root "release"
    $work = Join-Path $root ".package-work"
    $options = @("--noconfirm", "--clean", "--onedir", "--name", $name,
                 "--icon", (Join-Path $root "promotion_studio.ico"),
                 "--paths", (Join-Path $root "src"), "--collect-data", "tzdata", "--distpath", $dist,
                 "--workpath", $work, "--specpath", $work, (Join-Path $root "packaging/windows_entry.py"))
    $exe = Join-Path (Join-Path $dist $name) "$name.exe"
    $previousPath = $env:PATH
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
        & $pythonExe -m PyInstaller @options
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
        $env:PYTHONPATH = $null
        & $exe --package-smoke
        if ($LASTEXITCODE -ne 0) { throw "Packaged smoke test failed." }
    } finally {
        $env:PATH = $previousPath
        $env:PYTHONPATH = $previousPythonPath
    }
    $archive = Join-Path $dist "$name-Windows-x64.zip"
    Compress-Archive -LiteralPath (Join-Path $dist $name) -DestinationPath $archive -Force
    Get-FileHash -LiteralPath $archive -Algorithm SHA256
} finally {
    Pop-Location
}

