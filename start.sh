#!/usr/bin/env bash
# rkdub indítása Linuxon (a felület a böngészőben / alkalmazásablakban nyílik; ezt a terminált ne zárd be).
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
if [ ! -x .venv/bin/python ]; then
    echo "Még nincs telepítve: futtasd az ./install.sh-t."
    exit 1
fi
exec .venv/bin/python rkdub.py "$@"
