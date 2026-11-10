#!/usr/bin/env bash
# rkdub telepítő Linuxra: mindent a projektmappába tesz (Python, PyTorch CUDA-val, csomagok, modellek),
# beállítja a fordító AI-t (Ollama + a videókártyához illő gemma3 modell), és menüikont készít.
# NVIDIA-kártya nélkül processzoros módban telepít: a PyTorch processzoros változatát és a kis gemma3:4b-t.
# Rendszergazdai jog nem kell; csak az Ollama telepítése kér sudo-t (ha még nincs a gépen, és kéred).
#
# Használat: ./install.sh [--no-ollama] [--no-models] [--no-shortcut] [--ollama-model gemma3:12b] [--yes]
#            [--cpu]   (processzoros mód akkor is, ha van NVIDIA-kártya; a kipróbálásához)
set -euo pipefail

NO_OLLAMA=0; NO_MODELS=0; NO_SHORTCUT=0; YES=0; CPU=0; OLLAMA_MODEL=auto
while [ $# -gt 0 ]; do
    case "$1" in
        --no-ollama) NO_OLLAMA=1 ;;
        --no-models) NO_MODELS=1 ;;
        --no-shortcut) NO_SHORTCUT=1 ;;
        --yes|-y) YES=1 ;;
        --cpu) CPU=1 ;;
        --ollama-model) OLLAMA_MODEL="$2"; shift ;;
        --ollama-model=*) OLLAMA_MODEL="${1#*=}" ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "Ismeretlen kapcsoló: $1" >&2; exit 2 ;;
    esac
    shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$ROOT/.tools"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
TORCH_TAG=cu128
STEPS=7
mkdir -p "$TOOLS"
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$TOOLS/uv-cache}"
export HF_HUB_DISABLE_PROGRESS_BARS=1 HF_HUB_VERBOSITY=error TQDM_DISABLE=1

c_cyan=$'\e[36m'; c_yel=$'\e[33m'; c_red=$'\e[31m'; c_grn=$'\e[32m'; c_off=$'\e[0m'
step() { echo; echo "${c_cyan}[$1/$STEPS] $2${c_off}"; }
warn() { echo "${c_yel}$*${c_off}"; }
fail() { echo; echo "${c_red}HIBA: $*${c_off}"; echo "Ellenőrizd az internetkapcsolatot, majd futtasd újra (a kész részeket nem tölti le újra)."; exit 1; }
ask() {  # ask "kérdés" -> 0 = igen
    [ "$YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    read -r -p "$1 [i/N] " a; [[ "$a" =~ ^[iIyY] ]]
}
ollama_alive() { curl -fsS -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; }

echo "rkdub telepítése ide: $ROOT"
command -v curl >/dev/null || fail "a curl nincs telepítve (pl. sudo apt install curl)"

GPU_MIB=0
if command -v nvidia-smi >/dev/null; then
    GPU_MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc 0-9 || true)
    GPU_MIB=${GPU_MIB:-0}
    # a kártya generációja (compute capability, pl. 6.1 = GTX 10xx); a régebbit a PyTorch már nem támogatja
    GPU_CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || true)
    if [ "$GPU_MIB" -gt 0 ] && [[ "$GPU_CC" =~ ^[0-9]+\.[0-9]+$ ]] \
            && awk -v c="$GPU_CC" 'BEGIN { exit !(c < 6.1) }'; then
        warn "A videókártya túl régi a gyorsításhoz (compute capability $GPU_CC; a GTX 10xx-től megy)."
        GPU_MIB=0
    fi
fi
[ "$CPU" = 1 ] && GPU_MIB=0
if [ "$GPU_MIB" -gt 0 ]; then
    echo "Videókártya: NVIDIA, $((GPU_MIB / 1024)) GB videómemória"
else
    warn "Nem találtam NVIDIA-videókártyát (vagy hiányzik az NVIDIA-illesztő): processzoros módban telepítek."
    warn "A szinkron így is elkészül, csak lassabban (egy 15 perces videó kb. 3-8 óra)."
    TORCH_TAG=cpu                      # a PyTorch processzoros változata: 0,6 GB a CUDA-s 3,5 GB helyett
fi
TORCH_INDEX="https://download.pytorch.org/whl/$TORCH_TAG"
if [ "$OLLAMA_MODEL" = auto ]; then
    # a fordítás közben a teljes modellnek el kell férnie a videómemóriában
    if [ "$GPU_MIB" -ge 20000 ]; then OLLAMA_MODEL=gemma3:27b
    elif [ "$GPU_MIB" -ge 11000 ]; then OLLAMA_MODEL=gemma3:12b
    else OLLAMA_MODEL=gemma3:4b; fi
fi

# --- 1. uv -----------------------------------------------------------------------------------
step 1 "Telepítő eszköz (uv)..."
if [ -x "$TOOLS/uv" ]; then UV="$TOOLS/uv"
elif command -v uv >/dev/null; then UV="$(command -v uv)"
else
    arch=$(uname -m)
    curl -fL --retry 5 -o "$TOOLS/uv.tar.gz" \
        "https://github.com/astral-sh/uv/releases/latest/download/uv-${arch}-unknown-linux-gnu.tar.gz" || fail "az uv letöltése nem sikerült"
    tar -xzf "$TOOLS/uv.tar.gz" -C "$TOOLS" --strip-components=1
    rm -f "$TOOLS/uv.tar.gz"
    UV="$TOOLS/uv"
fi
"$UV" --version

# --- 2. Python ---------------------------------------------------------------------------------
step 2 "Python 3.12 és saját környezet (.venv)..."
"$UV" venv "$VENV" --python 3.12 --python-preference only-managed --seed --allow-existing || fail "a Python-környezet létrehozása nem sikerült"

# --- 3. PyTorch --------------------------------------------------------------------------------
if [ "$TORCH_TAG" = cpu ]; then step 3 "PyTorch (processzoros változat, kb. 0,6 GB)..."
else step 3 "PyTorch NVIDIA-gyorsítással (kb. 3,5 GB)..."; fi
CONSTRAINTS="$TOOLS/constraints.txt"; OVERRIDES="$TOOLS/overrides.txt"
printf 'torch==2.8.0+%s\ntorchaudio==2.8.0+%s\ntorchvision==0.23.0+%s\n' $TORCH_TAG $TORCH_TAG $TORCH_TAG > "$CONSTRAINTS"
# a docopt-ot csak a num2words parancssori része használja; kihagyjuk (a sosem teljesülő feltétel kiveszi)
echo "docopt ; sys_platform == 'never'" > "$OVERRIDES"
if "$PY" -c "import torch, torchaudio, sys; sys.exit(torch.__version__ != '2.8.0+$TORCH_TAG')" 2>/dev/null; then
    echo "  A PyTorch már telepítve van."
else
    "$UV" pip install --python "$PY" torch==2.8.0+$TORCH_TAG torchaudio==2.8.0+$TORCH_TAG --index-url "$TORCH_INDEX" \
        || fail "a PyTorch telepítése nem sikerült"
fi

# --- 4. csomagok -------------------------------------------------------------------------------
step 4 "Csomagok: hangmodell, beszédfelismerő, hangleválasztó, felület..."
"$UV" pip install --python "$PY" -r "$ROOT/requirements.txt" -c "$CONSTRAINTS" --override "$OVERRIDES" \
    --extra-index-url "$TORCH_INDEX" --index-strategy unsafe-best-match || fail "a csomagok telepítése nem sikerült"
if [ "$UV_CACHE_DIR" = "$TOOLS/uv-cache" ] && [ -d "$UV_CACHE_DIR" ]; then
    echo "  A telepítési gyorsítótár törlése..."
    "$UV" cache clean --cache-dir "$UV_CACHE_DIR" >/dev/null 2>&1 || true
fi
command -v ffmpeg >/dev/null || warn "  Nincs ffmpeg a gépen: a program a beépítettet használja (ajánlott: sudo apt install ffmpeg)."

# --- 5. Ollama ---------------------------------------------------------------------------------
if [ "$NO_OLLAMA" = 1 ]; then
    step 5 "Fordító AI: kihagyva (a beállításoknál DeepL-kulcs is megadható)."
else
    step 5 "Fordító AI: Ollama és a fordítómodell ($OLLAMA_MODEL)..."
    if ! command -v ollama >/dev/null; then
        echo "  Az Ollama nincs telepítve. A hivatalos telepítője (https://ollama.com/install.sh) sudo-jogot kér,"
        echo "  és rendszerszolgáltatásként állítja be."
        if ask "  Telepítsem most?"; then
            curl -fsSL https://ollama.com/install.sh | sh || fail "az Ollama telepítése nem sikerült"
        else
            warn "  Kihagyva. Később: curl -fsSL https://ollama.com/install.sh | sh  és  ollama pull $OLLAMA_MODEL"
        fi
    else
        echo "  Az Ollama már telepítve van."
    fi
    if command -v ollama >/dev/null; then
        if ! ollama_alive; then
            if systemctl is-enabled --quiet ollama 2>/dev/null; then
                echo "  Az Ollama-szolgáltatás indítása (sudo)..."
                sudo systemctl start ollama || true
            else
                (nohup ollama serve >/dev/null 2>&1 &)
            fi
            for _ in $(seq 90); do ollama_alive && break; sleep 1; done
            ollama_alive || fail "az Ollama nem indult el (próbáld: sudo systemctl start ollama, vagy ollama serve)"
        fi
        if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$OLLAMA_MODEL"; then
            echo "  A fordítómodell már megvan: $OLLAMA_MODEL"
        else
            echo "  A fordítómodell letöltése: $OLLAMA_MODEL (egyszeri)..."
            ollama pull "$OLLAMA_MODEL" || fail "a fordítómodell letöltése nem sikerült"
        fi
    fi
fi

# --- 6. modellek -------------------------------------------------------------------------------
if [ "$NO_MODELS" = 1 ]; then
    step 6 "Modellek: kihagyva (az első szinkronnál tölti le)."
else
    step 6 "Beszéd- és hangmodellek letöltése (egyszeri, kb. 9 GB)..."
    "$PY" -X utf8 "$ROOT/tools/download_models.py" || fail "a modellek letöltése nem sikerült"
fi

# --- 7. menüikon -------------------------------------------------------------------------------
step 7 "Menüikon..."
chmod +x "$ROOT/start.sh" "$ROOT/uninstall.sh" 2>/dev/null || true
START_ARGS=""
[ "$CPU" = 1 ] && START_ARGS=" --cpu"     # NVIDIA-kártyás gépen is processzoron (a kipróbált mód marad)
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
write_desktop() {
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=rkdub
Comment=rkdub - helyi magyar szinkron
Exec="$ROOT/start.sh"$START_ARGS
Path=$ROOT
Icon=$ROOT/rkdub.png
Terminal=true
Categories=AudioVideo;Video;
EOF
    chmod +x "$1"
}
write_desktop "$APPS/rkdub.desktop"
if [ "$NO_SHORTCUT" = 0 ]; then
    DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    if [ -d "$DESK" ]; then
        write_desktop "$DESK/rkdub.desktop"
        command -v gio >/dev/null && gio set "$DESK/rkdub.desktop" metadata::trusted true 2>/dev/null || true
    fi
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

echo
echo "${c_grn}Kész! Indítás: az alkalmazásmenüben az \"rkdub\", vagy: $ROOT/start.sh${c_off}"
[ "$NO_OLLAMA" = 1 ] && echo "${c_grn}A fordításhoz helyi AI (Ollama, https://ollama.com) vagy DeepL-kulcs kell (a beállításoknál).${c_off}"
exit 0
