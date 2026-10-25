# rkdub eltávolítása: ikonok, Windows-bejegyzés, beállítások, kérésre a letöltött modellek, végül a programmappa.
# Az Ollamához (és a fordítómodellekhez) nem nyúl: azt más programok is használhatják; külön eltávolítható
# a Windows Beállítások > Alkalmazások menüjében.

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot

Write-Host 'rkdub eltávolítása' -ForegroundColor Cyan
Write-Host "Mappa: $Root"
Write-Host ''
$ans = Read-Host 'Biztosan eltávolítod? A kész videók (kesz mappa) is törlődnek! (i/n)'
if ($ans -notmatch '^[iIyY]') { Write-Host 'Mégsem.'; exit 0 }

$kesz = Join-Path $Root 'kesz'
if ((Test-Path $kesz) -and (Get-ChildItem $kesz -ErrorAction SilentlyContinue)) {
    $save = Read-Host 'A kész fájlokat áthelyezzem a Dokumentumok\rkdub-kesz mappába, hogy megmaradjanak? (i/n)'
    if ($save -match '^[iIyY]') {
        $dst = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'rkdub-kesz'
        New-Item -ItemType Directory -Force $dst | Out-Null
        Get-ChildItem $kesz | Move-Item -Destination $dst -Force
        Write-Host "  Áthelyezve: $dst"
    }
}

foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    Remove-Item (Join-Path $dir 'rkdub.lnk') -ErrorAction SilentlyContinue
}
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\rkdub' -Recurse -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:USERPROFILE '.rkdub') -Recurse -Force -ErrorAction SilentlyContinue
Write-Host '  Ikonok, beállítások törölve.'

$hf = Join-Path $env:USERPROFILE '.cache\huggingface\hub'
$models = @('models--k2-fsa--OmniVoice', 'models--Systran--faster-whisper-large-v3',
            'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo', 'models--deepdml--faster-whisper-large-v3-turbo-ct2',
            'models--microsoft--wavlm-base-plus-sv') | ForEach-Object { Join-Path $hf $_ } | Where-Object { Test-Path $_ }
if ($models) {
    $del = Read-Host 'A letöltött beszéd- és hangmodelleket (kb. 6 GB, Hugging Face gyorsítótár) is törlöm? (i/n)'
    if ($del -match '^[iIyY]') {
        $models | ForEach-Object { Remove-Item $_ -Recurse -Force -ErrorAction SilentlyContinue }
        Write-Host '  Modellek törölve.'
    }
}

Write-Host ''
Write-Host 'Az Ollama és a fordítómodell megmaradt (más program is használhatja). Ha nem kell:'
Write-Host '  Windows Beállítások > Alkalmazások > Ollama > Eltávolítás.'
Write-Host ''
Write-Host 'A programmappa törlése...'
# a mappát egy külön folyamat törli, miután ez az ablak bezárult (a futó script nem törölheti magát)
Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "timeout /t 3 /nobreak >nul & rmdir /s /q `"$Root`"" -WindowStyle Hidden
Write-Host 'Kész. Az rkdub eltávolítva.' -ForegroundColor Green
Start-Sleep -Seconds 2
