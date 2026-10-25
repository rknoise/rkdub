# rkdub telepítő: mindent a projektmappába tesz (Python, PyTorch CUDA-val, csomagok, modellek), telepíti a
# fordító AI-t (Ollama + a videókártyához illő gemma3 modell), asztali ikont és eltávolítót készít.
# Előre telepített Python nem kell hozzá. Rendszergazdai jog sem kell (minden a felhasználóhoz kerül).
#
# Indítás: rkdub-telepito.exe (grafikus), install.bat, vagy:
#   powershell -ExecutionPolicy Bypass -File tools\install.ps1 [-NoOllama] [-NoModels] [-NoShortcut] [-OllamaModel gemma3:12b]
# A "##STEP", "##PCT", "##DONE", "##FAIL" kezdetű sorokat a grafikus telepítő dolgozza fel.

param([switch]$NoModels, [switch]$NoShortcut, [switch]$NoOllama, [string]$OllamaModel = 'auto')

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root '.tools'
$Venv = Join-Path $Root '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
New-Item -ItemType Directory -Force $Tools | Out-Null

$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $Tools 'uv-cache' }
$env:UV_LINK_MODE = 'copy'
$env:UV_NO_PROGRESS = '1'                    # a grafikus telepítő naplójában ne legyenek folyamatsávok
$env:HF_HUB_DISABLE_PROGRESS_BARS = '1'
$env:TQDM_DISABLE = '1'
$TorchIndex = 'https://download.pytorch.org/whl/cu128'
$Constraints = Join-Path $Tools 'constraints.txt'
$Steps = 7

function Step($n, $text) {
    Write-Host ''
    Write-Host "##STEP $n/$Steps $text"
    Write-Host "[$n/$Steps] $text" -ForegroundColor Cyan
}

function Pct($p) { Write-Host "##PCT $p" }

function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what nem sikerült (kilépési kód: $LASTEXITCODE)." }
}

function Native([scriptblock]$cmd) {
    # Külső program futtatása behúzott naplóval. A hibakimenetét (pl. az uv ott írja a normál üzeneteit is)
    # nem tekintjük hibának: a sikerességet a kilépési kód mutatja (Check).
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $cmd 2>&1 | ForEach-Object { "  $_" } } finally { $ErrorActionPreference = $old }
}

function Ollama-Alive {
    try { Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

function Ollama-Models {
    try { return @((Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5).models | ForEach-Object { $_.name }) }
    catch { return @() }
}

function Gpu-MemMiB {
    try {
        $smi = (Get-Command nvidia-smi -ErrorAction Stop).Source
        return [int](& $smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1)
    } catch { return 0 }
}

function Pull-Model($name) {
    # Az Ollama saját letöltője a HTTP API-n át: tiszta százalékos napló (az "ollama pull" konzolos animációja helyett)
    $req = [Net.HttpWebRequest]::Create('http://127.0.0.1:11434/api/pull')
    $req.Method = 'POST'
    $req.ContentType = 'application/json'
    $req.Timeout = 60000
    $req.ReadWriteTimeout = 3600000
    $body = [Text.Encoding]::UTF8.GetBytes((@{ model = $name; stream = $true } | ConvertTo-Json -Compress))
    $s = $req.GetRequestStream(); $s.Write($body, 0, $body.Length); $s.Close()
    $reader = New-Object IO.StreamReader($req.GetResponse().GetResponseStream())
    $lastPct = -10; $lastStatus = ''
    while (-not $reader.EndOfStream) {
        $line = $reader.ReadLine()
        if (-not $line) { continue }
        $j = $line | ConvertFrom-Json
        if ($j.error) { throw "a modell letöltése nem sikerült: $($j.error)" }
        if ($j.total -and $j.completed) {
            $p = [int](100 * $j.completed / $j.total)
            if ($j.total -gt 100MB -and ($p -ge $lastPct + 5 -or $p -eq 100)) {
                $lastPct = $p
                Pct $p
                Write-Host ("  {0}: {1}% ({2:N1} / {3:N1} GB)" -f $name, $p, ($j.completed / 1GB), ($j.total / 1GB))
            }
        } elseif ($j.status -and $j.status -ne $lastStatus -and $j.status -notmatch '^pulling [0-9a-f]{6,}') {
            $lastStatus = $j.status
            Write-Host "  $($j.status)"
        }
    }
}

try {
    Write-Host "rkdub telepítése ide: $Root"
    if ($Root -match '[^\x00-\x7F]') {
        Write-Host 'Figyelem: ékezet van a mappa útvonalában. Ha hibát látsz, tedd a mappát pl. ide: C:\rkdub' -ForegroundColor Yellow
    }
    $gpuMiB = Gpu-MemMiB
    if ($gpuMiB -gt 0) {
        Write-Host ("Videókártya: NVIDIA, {0:N0} GB videómemória" -f ($gpuMiB / 1024))
    } else {
        Write-Host 'Figyelem: nem találtam NVIDIA videokártyát. A program CPU-n is elindul, de nagyon lassú lesz.' -ForegroundColor Yellow
    }

    Step 1 'Telepítő eszköz (uv) letöltése...'
    $uv = Join-Path $Tools 'uv.exe'
    if (-not (Test-Path $uv)) {
        $zip = Join-Path $Tools 'uv.zip'
        Invoke-WebRequest -UseBasicParsing -OutFile $zip `
            -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
        Expand-Archive -Path $zip -DestinationPath $Tools -Force
        Remove-Item $zip
    }
    Native { & $uv --version }
    Check 'A uv indítása'

    Step 2 'Python 3.12 és saját környezet (.venv)...'
    Native { & $uv venv $Venv --python 3.12 --python-preference only-managed --seed --allow-existing }
    Check 'A Python-környezet létrehozása'

    Step 3 'PyTorch NVIDIA-gyorsítással (kb. 3 GB, pár perc)...'
    Set-Content -Encoding ascii $Constraints "torch==2.8.0+cu128`ntorchaudio==2.8.0+cu128`ntorchvision==0.23.0+cu128"
    Native { & $uv pip install --python $Py torch==2.8.0+cu128 torchaudio==2.8.0+cu128 --index-url $TorchIndex }
    Check 'A PyTorch telepítése'

    Step 4 'Csomagok: hangmodell, beszédfelismerő, hangleválasztó, felület...'
    Native { & $uv pip install --python $Py -r (Join-Path $Root 'requirements.txt') -c $Constraints `
        --extra-index-url $TorchIndex --index-strategy unsafe-best-match }
    Check 'A csomagok telepítése'

    if (-not $NoOllama) {
        Step 5 'Fordító AI: Ollama és a fordítómodell...'
        $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
        if (-not (Test-Path $app) -and -not (Get-Command ollama -ErrorAction SilentlyContinue)) {
            Write-Host '  Az Ollama letöltése (ollama.com, kb. 1 GB)...'
            $setup = Join-Path $Tools 'OllamaSetup.exe'
            Invoke-WebRequest -UseBasicParsing -OutFile $setup -Uri 'https://ollama.com/download/OllamaSetup.exe'
            Write-Host '  Az Ollama telepítése...'
            Start-Process -FilePath $setup -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES' -Wait
            Remove-Item $setup -ErrorAction SilentlyContinue
        } else {
            Write-Host '  Az Ollama már telepítve van.'
        }
        if (-not (Ollama-Alive)) {
            # az alkalmazást indítjuk (nem csupasz "ollama serve"-t): így az ott beállított modellmappát használja
            if (Test-Path $app) { Start-Process -FilePath $app } else { Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden }
            for ($i = 0; $i -lt 90 -and -not (Ollama-Alive); $i++) { Start-Sleep -Seconds 1 }
            if (-not (Ollama-Alive)) { throw 'az Ollama nem indult el (próbáld elindítani a Start menüből, majd futtasd újra a telepítőt)' }
            Start-Sleep -Seconds 2
        }
        if ($OllamaModel -eq 'auto') {
            # a videókártyához illő méret: a fordítás közben a teljes modellnek el kell férnie a videómemóriában
            if ($gpuMiB -ge 20000) { $OllamaModel = 'gemma3:27b' } elseif ($gpuMiB -ge 11000) { $OllamaModel = 'gemma3:12b' } else { $OllamaModel = 'gemma3:4b' }
        }
        $have = Ollama-Models
        if ($have -contains $OllamaModel) {
            Write-Host "  A fordítómodell már megvan: $OllamaModel"
        } else {
            Write-Host "  A fordítómodell letöltése: $OllamaModel (egyszeri; a 27b kb. 17 GB)..."
            Pull-Model $OllamaModel
        }
    } else {
        Step 5 'Fordító AI: kihagyva (a beállításoknál DeepL-kulcs is megadható).'
    }

    if (-not $NoModels) {
        Step 6 'Modellek letöltése (egyszeri, kb. 6 GB)...'
        Native { & $Py -X utf8 (Join-Path $Root 'tools\download_models.py') }
        Check 'A modellek letöltése'
    } else {
        Step 6 'Modellek: kihagyva (az első szinkronnál tölti le).'
    }

    Step 7 'Asztali ikon, Start menü, eltávolító...'
    $ws = New-Object -ComObject WScript.Shell
    $dirs = @([Environment]::GetFolderPath('Programs'))
    if (-not $NoShortcut) { $dirs += [Environment]::GetFolderPath('Desktop') }
    foreach ($dir in $dirs) {
        $lnk = $ws.CreateShortcut((Join-Path $dir 'rkdub.lnk'))
        $lnk.TargetPath = Join-Path $Root 'start.bat'
        $lnk.WorkingDirectory = $Root
        $lnk.WindowStyle = 7     # kis méretű konzolablak (a felület külön ablakban nyílik)
        $lnk.IconLocation = (Join-Path $Root 'rkdub.ico') + ',0'
        $lnk.Description = 'rkdub - helyi magyar szinkron'
        $lnk.Save()
    }
    # megjelenik a Windows "Telepített alkalmazások" listájában (csak a saját felhasználónál)
    $version = (Select-String -Path (Join-Path $Root 'dub\__init__.py') -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\rkdub'
    New-Item -Path $key -Force | Out-Null
    $vals = @{
        DisplayName = 'rkdub - helyi magyar szinkron'; DisplayVersion = $version; Publisher = 'rkdub'
        DisplayIcon = (Join-Path $Root 'rkdub.ico'); InstallLocation = $Root
        UninstallString = ('"' + (Join-Path $Root 'eltavolitas.bat') + '"')
    }
    foreach ($k in $vals.Keys) { Set-ItemProperty -Path $key -Name $k -Value $vals[$k] }
    Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
    Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord
    Set-ItemProperty -Path $key -Name EstimatedSize -Value 8000000 -Type DWord

    Write-Host ''
    Write-Host 'Kész! Indítás: az asztali "rkdub" ikon (vagy a start.bat).' -ForegroundColor Green
    if ($NoOllama) {
        Write-Host 'A fordításhoz helyi AI kell (Ollama, https://ollama.com), vagy DeepL-kulcs (a beállításoknál adható meg).' -ForegroundColor Green
    }
    Write-Host '##DONE'
    exit 0
}
catch {
    Write-Host ''
    Write-Host "HIBA: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Ellenőrizd az internetkapcsolatot, majd futtasd újra a telepítőt (a már letöltött részeket nem tölti le újra).'
    Write-Host "##FAIL $($_.Exception.Message)"
    exit 1
}
