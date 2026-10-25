# Az egyfájlos telepítő (dist\rkdub-telepito.exe) elkészítése: a program fájljai egy zip-be kerülnek, amit a
# telepítő beágyazva visz magával. A fordításhoz a Windows beépített .NET Framework 4 C#-fordítója elég.
# Futtatás: powershell -ExecutionPolicy Bypass -File tools\build_installer.ps1

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $Root '.tools\build'
$Dist = Join-Path $Root 'dist'
$Csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
New-Item -ItemType Directory -Force $Build, $Dist | Out-Null

# 1. a program fájljai (a helyi környezet, modellek, munkafájlok és a kész videók nélkül)
$include = @('dub', 'tools', 'minta', 'docs', 'rkdub.py', 'start.bat', 'install.bat', 'eltavolitas.bat', 'requirements.txt',
             'README.md', 'rkdub.ico', 'rkdub.png', '.gitignore')
$stage = Join-Path $Build 'payload'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
foreach ($item in $include) {
    $src = Join-Path $Root $item
    if (Test-Path $src) { Copy-Item $src -Destination $stage -Recurse -Force }
}
Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Get-ChildItem (Join-Path $stage 'tools') -Recurse -File | Where-Object { $_.Extension -in '.exe', '.zip' } | Remove-Item -Force
Remove-Item (Join-Path $stage 'tools\installer') -Recurse -Force -ErrorAction SilentlyContinue
$zip = Join-Path $Build 'payload.zip'
Remove-Item $zip -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
Write-Host ("Csomag: {0:N1} MB, {1} fájl" -f ((Get-Item $zip).Length / 1MB), (Get-ChildItem $stage -Recurse -File).Count)

# 2. a telepítő exe
$out = Join-Path $Dist 'rkdub-telepito.exe'
$refs = 'System.dll,System.Core.dll,System.Drawing.dll,System.Windows.Forms.dll,System.IO.Compression.dll,System.IO.Compression.FileSystem.dll'
& $Csc /nologo /target:winexe /optimize+ /codepage:65001 "/out:$out" "/win32icon:$(Join-Path $Root 'rkdub.ico')" `
    "/resource:$zip,payload.zip" "/reference:$refs" (Join-Path $PSScriptRoot 'installer\RkdubSetup.cs')
if ($LASTEXITCODE -ne 0) { throw "a fordítás nem sikerült ($LASTEXITCODE)" }
Write-Host ("Kész: {0} ({1:N1} MB)" -f $out, ((Get-Item $out).Length / 1MB)) -ForegroundColor Green
