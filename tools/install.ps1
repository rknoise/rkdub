# rkdub telepítő: mindent a projektmappába tesz (Python, PyTorch CUDA-val, csomagok, modellek), telepíti a
# fordító AI-t (Ollama + a videókártyához illő gemma3 modell), asztali ikont és eltávolítót készít.
# Előre telepített Python nem kell hozzá. Rendszergazdai jog sem kell (minden a felhasználóhoz kerül).
#
# Indítás: rkdub-telepito.exe (grafikus), install.bat, vagy:
#   powershell -ExecutionPolicy Bypass -File tools\install.ps1 [-NoOllama] [-NoModels] [-NoShortcut] [-OllamaModel gemma3:12b]
# A grafikus telepítő (RKDUB_GUI=1) a "##" kezdetű sorokból rajzolja a folyamatjelzőt:
#   ##PLAN azonosító bájt név   a telepítés egy része (induláskor mindet felsoroljuk)
#   ##PART azonosító            ez a rész indul
#   ##GOT azonosító kész [összes]   eddig letöltött bájtok
#   ##WORK azonosító szöveg     a rész most nem tölt, hanem mást csinál (pl. kicsomagol)
#   ##SKIP azonosító            ez a rész már megvan
#   ##END azonosító             ez a rész kész
#   ##DONE, ##FAIL szöveg       a telepítés vége

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
$env:HF_HUB_DISABLE_PROGRESS_BARS = '1'
$env:HF_HUB_VERBOSITY = 'error'
$env:TQDM_DISABLE = '1'
$TorchIndex = 'https://download.pytorch.org/whl/cu128'
$TorchWheel = 'torch-2.8.0+cu128-cp312-cp312-win_amd64.whl'
$TorchSha256 = '0ad925202387f4e7314302a1b4f8860fa824357f9b1466d7992bf276370ebcff'
$Constraints = Join-Path $Tools 'constraints.txt'
$Steps = 7
$Gui = $env:RKDUB_GUI -eq '1'
$OllamaBytes = @{ 'gemma3:27b' = 17413409652; 'gemma3:12b' = 8164039732; 'gemma3:4b' = 3350272403 }

function Emit($line) { if ($Gui) { Write-Host "##$line" } }

function Step($n, $text) {
    Write-Host ''
    Write-Host "[$n/$Steps] $text" -ForegroundColor Cyan
}

function Part-Work($id, $text) {
    Emit "WORK $id $text"
    Write-Host "  $text"
}

function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what nem sikerült (kilépési kód: $LASTEXITCODE)." }
}

function Native([scriptblock]$cmd) {
    # Külső program futtatása behúzott naplóval. A hibakimenetét (pl. az uv ott írja a normál üzeneteit is)
    # nem tekintjük hibának: a sikerességet a kilépési kód mutatja (Check).
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $cmd 2>&1 | ForEach-Object {
            $l = "$_"
            if ($l.StartsWith('##')) { $l } else { "  $l" }
        }
    } finally { $ErrorActionPreference = $old }
}

function Uv-Run($id, [scriptblock]$cmd) {
    # Mint a Native, de a uv soraiból követi a letöltést: nem terminálban csomagonként ír
    # ("Downloading numpy (12.0MiB)", majd " Downloaded numpy"), az 1 MB alattiakról nem szól.
    $unit = @{ KiB = 1024; MiB = 1048576; GiB = 1073741824 }
    $sizes = @{}
    $script:UvGot = [long]0
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $cmd 2>&1 | ForEach-Object {
            $l = "$_"
            if ($l -match '^\s*Downloading (.+) \(([\d.]+)(KiB|MiB|GiB)\)\s*$') {
                $sizes[$Matches[1]] = [long]([double]$Matches[2] * $unit[$Matches[3]])
            } elseif ($l -match '^\s*Downloaded (.+?)\s*$') {
                $script:UvGot += [long]$sizes[$Matches[1]]
                Emit "GOT $id $($script:UvGot)"
            } elseif ($l -match '^\s*Prepared ') {
                Part-Work $id 'A csomagok másolása a helyükre...'
            } else {
                "  $l"
            }
        }
    } finally { $ErrorActionPreference = $old }
}

function Download-File($id, $name, $url, $path) {
    # Saját letöltő: bájtpontosan jelzi, hol tart, és megszakadás után a .part fájlból folytatja.
    if (Test-Path $path) { return }
    $tmp = "$path.part"
    $buf = New-Object byte[] 1048576
    for ($try = 1; ; $try++) {
        $have = [long]0
        $done = [long]0
        $resp = $null
        $out = $null
        try {
            if (Test-Path $tmp) { $have = (Get-Item $tmp).Length }
            $req = [Net.HttpWebRequest]::Create($url)
            $req.UserAgent = 'rkdub-telepito'
            $req.Timeout = 60000
            $req.ReadWriteTimeout = 60000
            if ($have -gt 0) { $req.AddRange($have) }
            $resp = $req.GetResponse()
            if ($resp.StatusCode -ne [Net.HttpStatusCode]::PartialContent) { $have = [long]0 }
            $total = $have + $resp.ContentLength
            $mode = if ($have -gt 0) { [IO.FileMode]::Append } else { [IO.FileMode]::Create }
            $out = New-Object IO.FileStream($tmp, $mode, [IO.FileAccess]::Write)
            $in = $resp.GetResponseStream()
            $done = $have
            $clock = [Diagnostics.Stopwatch]::StartNew()
            $sent = 0
            $logged = -10
            while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                $out.Write($buf, 0, $n)
                $done += $n
                if ($clock.ElapsedMilliseconds - $sent -ge 250) {
                    $sent = $clock.ElapsedMilliseconds
                    Emit "GOT $id $done $total"
                    $pct = [int][Math]::Floor(100 * $done / $total)
                    if ($total -gt 100MB -and $pct -ge $logged + 10) {
                        $logged = $pct - $pct % 10
                        Write-Host ("  {0}: {1}% ({2:N1} / {3:N1} GB, {4:N0} MB/s)" -f $name, $pct, ($done / 1e9), ($total / 1e9), (($done - $have) / 1e6 / [Math]::Max(0.001, $clock.Elapsed.TotalSeconds)))
                    }
                }
            }
            $out.Close()
            $out = $null
            if ($done -ne $total) { throw "nem teljes a letöltés ($done / $total bájt)" }
            Emit "GOT $id $done $total"
            Move-Item -Force $tmp $path
            return
        } catch {
            $e = $_.Exception
            if ($e.InnerException) { $e = $e.InnerException }
            if ($e.Response -and [int]$e.Response.StatusCode -eq 416) { Remove-Item $tmp -ErrorAction SilentlyContinue }
            if ($done -gt $have) { $try = 0 }                # haladt: nem számít sikertelen próbának
            if ($try -ge 6) { throw }
            Write-Host "  ${name}: megszakadt a letöltés ($($e.Message)), újra próbálom..."
            Start-Sleep -Seconds 5
        } finally {
            if ($out) { $out.Close() }
            if ($resp) { $resp.Close() }
        }
    }
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
    $layers = @{}                                    # az Ollama rétegenként jelent: digest -> kész bájtok
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $sent = 0
    while (-not $reader.EndOfStream) {
        $line = $reader.ReadLine()
        if (-not $line) { continue }
        $j = $line | ConvertFrom-Json
        if ($j.error) { throw "a modell letöltése nem sikerült: $($j.error)" }
        if ($j.total -and $j.completed) {
            $layers[$j.digest] = [long]$j.completed
            $done = [long]0
            foreach ($v in $layers.Values) { $done += $v }
            if ($clock.ElapsedMilliseconds - $sent -ge 250) {
                $sent = $clock.ElapsedMilliseconds
                Emit "GOT model $done"
            }
            $p = [int](100 * $j.completed / $j.total)
            if ($j.total -gt 100MB -and ($p -ge $lastPct + 5 -or $p -eq 100)) {
                $lastPct = $p
                Write-Host ("  {0}: {1}% ({2:N1} / {3:N1} GB)" -f $name, $p, ($j.completed / 1e9), ($j.total / 1e9))
            }
        } elseif ($j.status -and $j.status -ne $lastStatus -and $j.status -notmatch '^pulling [0-9a-f]{6,}') {
            $lastStatus = $j.status
            Write-Host "  $($j.status)"
            if ($j.status -match 'verifying') { Emit 'WORK model A letöltött modell ellenőrzése...' }
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
    if ($OllamaModel -eq 'auto') {
        # a videókártyához illő méret: a fordítás közben a teljes modellnek el kell férnie a videómemóriában
        if ($gpuMiB -ge 20000) { $OllamaModel = 'gemma3:27b' } elseif ($gpuMiB -ge 11000) { $OllamaModel = 'gemma3:12b' } else { $OllamaModel = 'gemma3:4b' }
    }

    # a részek várható mérete (bájt); a letöltők a pontos értékkel felülírják
    Emit 'PLAN uv 15000000 Telepítő eszköz (uv)'
    Emit 'PLAN python 22000000 Python 3.12'
    Emit 'PLAN torch 3461000000 PyTorch (NVIDIA-gyorsítás)'
    Emit 'PLAN packages 320000000 Python-csomagok'
    if (-not $NoOllama) {
        Emit 'PLAN ollama 1680000000 Ollama (a fordító AI futtatója)'
        $modelBytes = $OllamaBytes[$OllamaModel]
        if (-not $modelBytes) { $modelBytes = 3350272403 }
        Emit "PLAN model $modelBytes Fordítómodell ($OllamaModel)"
    }
    if (-not $NoModels) {
        Emit 'PLAN omnivoice 3267000000 OmniVoice (magyar hang)'
        Emit 'PLAN whisper_turbo 1622000000 Whisper large-v3-turbo (beszédfelismerés)'
        Emit 'PLAN whisper_v3 3091000000 Whisper large-v3 (hangellenőrzés)'
        Emit 'PLAN wavlm 405000000 WavLM (tartalék beszélőazonosító)'
        Emit 'PLAN ecapa 89000000 ECAPA (beszélők azonosítása)'
        Emit 'PLAN roformer 639000000 BS-Roformer (beszéd és háttér szétválasztása)'
    }
    Emit 'PLAN final 0 Asztali ikon, Start menü, eltávolító'

    Step 1 'Telepítő eszköz (uv)...'
    $uv = Join-Path $Tools 'uv.exe'
    if (Test-Path $uv) {
        Emit 'SKIP uv'
    } else {
        Emit 'PART uv'
        $zip = Join-Path $Tools 'uv.zip'
        Download-File 'uv' 'uv' 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' $zip
        Expand-Archive -Path $zip -DestinationPath $Tools -Force
        Remove-Item $zip
        Emit 'END uv'
    }
    Native { & $uv --version }
    Check 'A uv indítása'

    Step 2 'Python 3.12 és saját környezet (.venv)...'
    if (Test-Path $Py) { Emit 'SKIP python' } else { Emit 'PART python' }
    Uv-Run 'python' { & $uv venv $Venv --python 3.12 --python-preference only-managed --seed --allow-existing }
    Check 'A Python-környezet létrehozása'
    Emit 'END python'

    Step 3 'PyTorch NVIDIA-gyorsítással...'
    Set-Content -Encoding ascii $Constraints "torch==2.8.0+cu128`ntorchaudio==2.8.0+cu128`ntorchvision==0.23.0+cu128"
    $site = Join-Path $Venv 'Lib\site-packages'
    if ((Test-Path (Join-Path $site 'torch-2.8.0+cu128.dist-info')) -and (Test-Path (Join-Path $site 'torchaudio-2.8.0+cu128.dist-info'))) {
        Write-Host '  A PyTorch már telepítve van.'
        Emit 'SKIP torch'
    } else {
        # a torch önmagában 3,5 GB: külön töltjük le, így pontos a folyamatjelző, és megszakadás után folytatható
        Emit 'PART torch'
        $whl = Join-Path $Tools $TorchWheel
        Download-File 'torch' 'PyTorch' "$TorchIndex/$($TorchWheel -replace '\+', '%2B')" $whl
        Part-Work 'torch' 'A letöltött fájl ellenőrzése...'
        if ((Get-FileHash $whl -Algorithm SHA256).Hash -ne $TorchSha256) {
            Remove-Item $whl
            throw 'a PyTorch letöltött fájlja hibás, futtasd újra a telepítőt'
        }
        Part-Work 'torch' 'Kicsomagolás, ez több percig is eltarthat...'
        Native { & $uv pip install --python $Py $whl torchaudio==2.8.0+cu128 --index-url $TorchIndex }
        Check 'A PyTorch telepítése'
        Remove-Item $whl
        Emit 'END torch'
    }

    Step 4 'Csomagok: hangmodell, beszédfelismerő, hangleválasztó, felület...'
    Emit 'PART packages'
    Uv-Run 'packages' { & $uv pip install --python $Py -r (Join-Path $Root 'requirements.txt') -c $Constraints `
        --extra-index-url $TorchIndex --index-strategy unsafe-best-match }
    Check 'A csomagok telepítése'
    # a csomagok másolatként vannak a .venv-ben, a saját gyorsítótárunk (kb. 8 GB) már csak a helyet foglalná
    $cache = Join-Path $Tools 'uv-cache'
    if (Test-Path $cache) {
        Part-Work 'packages' 'A telepítési gyorsítótár törlése...'
        Native { & $uv cache clean --cache-dir $cache }
    }
    if ($script:UvGot -gt 0) { Emit 'END packages' } else { Emit 'SKIP packages' }

    if (-not $NoOllama) {
        Step 5 'Fordító AI: Ollama és a fordítómodell...'
        $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
        if (-not (Test-Path $app) -and -not (Get-Command ollama -ErrorAction SilentlyContinue)) {
            Emit 'PART ollama'
            $setup = Join-Path $Tools 'OllamaSetup.exe'
            Download-File 'ollama' 'Ollama' 'https://ollama.com/download/OllamaSetup.exe' $setup
            Part-Work 'ollama' 'Az Ollama telepítése...'
            Start-Process -FilePath $setup -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES' -Wait
            Remove-Item $setup -ErrorAction SilentlyContinue
            Emit 'END ollama'
        } else {
            Write-Host '  Az Ollama már telepítve van.'
            Emit 'SKIP ollama'
        }
        if (-not (Ollama-Alive)) {
            # az alkalmazást indítjuk (nem csupasz "ollama serve"-t): így az ott beállított modellmappát használja
            if (Test-Path $app) { Start-Process -FilePath $app } else { Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden }
            for ($i = 0; $i -lt 90 -and -not (Ollama-Alive); $i++) { Start-Sleep -Seconds 1 }
            if (-not (Ollama-Alive)) { throw 'az Ollama nem indult el (próbáld elindítani a Start menüből, majd futtasd újra a telepítőt)' }
            Start-Sleep -Seconds 2
        }
        $have = Ollama-Models
        if ($have -contains $OllamaModel) {
            Write-Host "  A fordítómodell már megvan: $OllamaModel"
            Emit 'SKIP model'
        } else {
            Write-Host "  A fordítómodell letöltése: $OllamaModel (egyszeri)..."
            Emit 'PART model'
            Pull-Model $OllamaModel
            Emit 'END model'
        }
    } else {
        Step 5 'Fordító AI: kihagyva (a beállításoknál DeepL-kulcs is megadható).'
    }

    if (-not $NoModels) {
        Step 6 'Beszéd- és hangmodellek letöltése (egyszeri, kb. 9 GB)...'
        Native { & $Py -X utf8 (Join-Path $Root 'tools\download_models.py') }
        Check 'A modellek letöltése'
    } else {
        Step 6 'Modellek: kihagyva (az első szinkronnál tölti le).'
    }

    Step 7 'Asztali ikon, Start menü, eltávolító...'
    Emit 'PART final'
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
    Emit 'END final'

    Write-Host ''
    Write-Host 'Kész! Indítás: az asztali "rkdub" ikon (vagy a start.bat).' -ForegroundColor Green
    if ($NoOllama) {
        Write-Host 'A fordításhoz helyi AI kell (Ollama, https://ollama.com), vagy DeepL-kulcs (a beállításoknál adható meg).' -ForegroundColor Green
    }
    Emit 'DONE'
    exit 0
}
catch {
    Write-Host ''
    Write-Host "HIBA: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Ellenőrizd az internetkapcsolatot, majd futtasd újra a telepítőt (a kész részeket nem tölti le újra).'
    Emit "FAIL $($_.Exception.Message)"
    exit 1
}
