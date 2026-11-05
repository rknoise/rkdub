#!/usr/bin/env bash
# rkdub eltávolítása Linuxon: menüikon, saját környezet (.venv, .tools), igény szerint a modellek és a munkafájlok.
# Az Ollama és a fordítómodell megmarad (más program is használhatja): sudo systemctl disable --now ollama stb.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ask() { read -r -p "$1 [i/N] " a; [[ "$a" =~ ^[iIyY] ]]; }

rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/rkdub.desktop"
rm -f "$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")/rkdub.desktop"
rm -rf "$ROOT/.venv" "$ROOT/.tools"
echo "A program környezete és a menüikon törölve."
if [ -d "$ROOT/.models" ] && ask "A letöltött modelleket (.models és ~/.cache/huggingface) is törlöd?"; then
    rm -rf "$ROOT/.models"
    for d in k2-fsa--OmniVoice mobiuslabsgmbh--faster-whisper-large-v3-turbo Systran--faster-whisper-large-v3 \
             microsoft--wavlm-base-plus-sv speechbrain--spkrec-ecapa-voxceleb; do
        rm -rf "$HOME/.cache/huggingface/hub/models--$d"
    done
fi
if [ -d "$ROOT/munkak" ] && ask "A munkafájlokat (munkak) is törlöd?"; then rm -rf "$ROOT/munkak" "$ROOT/feltoltes"; fi
[ -d "$ROOT/kesz" ] && echo "A kész videók megmaradtak: $ROOT/kesz"
echo "Kész. A projektmappa ($ROOT) kézzel törölhető."
