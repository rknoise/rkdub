# -*- coding: utf-8 -*-
"""A telepítő hívja: előre letölti a modelleket, hogy az első szinkron ne várjon rájuk."""

import os
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GUI = os.environ.get("RKDUB_GUI") == "1"
WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]   # mint a faster-whisper


def emit(line):
    """Sor a grafikus telepítőnek (a formátumot lásd az install.ps1 elején)."""
    if GUI:
        print("##" + line, flush=True)


class Reporter:
    """Egy rész letöltött bájtjai: a grafikus telepítőnek negyedmásodpercenként, a naplóba 10%-onként."""

    def __init__(self, part, name, total, base=0):
        self.part, self.name, self.total, self.base = part, name, total, base
        self.done = base
        self.start = time.monotonic()
        self.sent = 0.0
        self.logged = -10
        self.lock = threading.Lock()

    def add(self, n):
        with self.lock:
            self.set(self.done + n)

    def set(self, done):
        self.done = done
        now = time.monotonic()
        if now - self.sent >= 0.25 or done >= self.total:
            self.sent = now
            emit(f"GOT {self.part} {done} {self.total}")
        pct = int(100 * done / self.total) if self.total else 0
        if pct >= self.logged + 10:
            self.logged = pct - pct % 10
            speed = (done - self.base) / 1e6 / max(now - self.start, 0.001)
            print(f"  {self.name}: {pct}% ({done / 1e9:.1f} / {self.total / 1e9:.1f} GB, {speed:.0f} MB/s)", flush=True)


def byte_bar(rep):
    """A huggingface_hub folyamatjelzője helyett: az összesített "Reconstructing" sáv a lemezre került bájtokat
    számolja, a Xet- és a sima HTTP-letöltésnél is. A többi metódust (refresh, close...) nem használjuk."""

    class Bar:
        format_dict = {}

        def __init__(self, *args, **kwargs):
            self.desc = kwargs.get("desc") or ""
            self.total = kwargs.get("total")
            self.n = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def update(self, n=1):
            self.n += n or 0
            if self.desc.startswith("Reconstructing"):
                rep.add(n or 0)

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    return Bar


def fetch_repo(part, name, repo, files=None):
    from huggingface_hub import snapshot_download

    missing = [f for f in snapshot_download(repo, allow_patterns=files, dry_run=True) if f.will_download]
    if not missing:
        emit(f"SKIP {part}")
        print(f"  {name}: már megvan.", flush=True)
        return
    emit(f"PART {part}")
    print(f"  {name}...", flush=True)
    rep = Reporter(part, name, sum(f.file_size or 0 for f in missing))
    snapshot_download(repo, allow_patterns=files, tqdm_class=byte_bar(rep))
    emit(f"END {part}")


def fetch_url(url, path, part=None, name=""):
    """Az audio-separator letöltője helyett: .part fájlba ír, így megszakadás után onnan folytatja, és csonka
    fájl nem kerülhet a végleges néven a helyére. Közben jelenti, hol tart."""
    import requests

    if os.path.isfile(path):
        return
    tmp = path + ".part"
    for attempt in range(6):
        have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        try:
            with requests.get(url, headers={"Range": f"bytes={have}-"} if have else {}, stream=True, timeout=300) as r:
                if r.status_code == 416:                 # a .part már nem illik a szerveren lévő fájlhoz
                    os.remove(tmp)
                    continue
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"Failed to download file from {url}, response code: {r.status_code}")
                if r.status_code == 200:
                    have = 0
                size = 0 if "content-encoding" in r.headers else int(r.headers.get("content-length", 0))
                total = have + size
                rep = Reporter(part, name, total, have) if part else None
                with open(tmp, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        if rep:
                            rep.set(have)
            if size and have != total:
                raise ConnectionError(f"nem teljes a letöltés ({have} / {total} bájt)")
            os.replace(tmp, path)
            return
        except (requests.RequestException, OSError) as e:
            print(f"  {name or url}: megszakadt a letöltés ({e}), újra próbálom...", flush=True)
            time.sleep(5)
    raise RuntimeError(f"Failed to download file from {url}")


def main():
    from faster_whisper.utils import _MODELS

    from dub.speakers import ECAPA_MODEL, SV_MODEL, SpeakerEmbedder

    fetch_repo("omnivoice", "OmniVoice (magyar hang)", "k2-fsa/OmniVoice")
    fetch_repo("whisper_turbo", "Whisper large-v3-turbo (beszédfelismerés)", _MODELS["large-v3-turbo"], WHISPER_FILES)
    fetch_repo("whisper_v3", "Whisper large-v3 (a kész magyar sorok ellenőrzése)", _MODELS["large-v3"], WHISPER_FILES)
    fetch_repo("wavlm", "WavLM (tartalék beszélőazonosító)", SV_MODEL)
    fetch_repo("ecapa", "ECAPA (beszélők azonosítása)", ECAPA_MODEL)
    SpeakerEmbedder().close()              # az ECAPA-modellt a .models\ecapa mappába teszi

    import logging

    from dub.util import ensure_ffmpeg_on_path

    ensure_ffmpeg_on_path()               # nélküle az audio-separator leáll, ha a gépen nincs ffmpeg
    from audio_separator.separator import Separator

    from dub.separate import MODEL_DIR, SEP_MODEL, drop_broken_model

    name = "BS-Roformer (beszéd és háttér szétválasztása)"
    os.makedirs(MODEL_DIR, exist_ok=True)
    drop_broken_model()                   # egy korábbi, megszakadt telepítés maradéka
    emit("SKIP roformer" if os.path.isfile(os.path.join(MODEL_DIR, SEP_MODEL)) else "PART roformer")
    print(f"  {name}...", flush=True)
    sep = Separator(model_file_dir=MODEL_DIR, log_level=logging.ERROR, output_dir=MODEL_DIR)
    sep.download_file_if_not_exists = lambda url, path: (
        fetch_url(url, path, "roformer", name) if url.endswith(SEP_MODEL) else fetch_url(url, path))
    sep.download_model_files(SEP_MODEL) if hasattr(sep, "download_model_files") else sep.load_model(SEP_MODEL)
    if drop_broken_model():
        raise RuntimeError("a BS-Roformer letöltése nem lett teljes")
    sep.list_supported_model_files()      # a modellista is helyben legyen (internet nélküli futáshoz)
    emit("END roformer")

    from dub.util import MODELS_OK

    with open(MODELS_OK, "w", encoding="utf-8") as f:   # innentől a program nem kérdezi le az internetet
        f.write("A telepítő minden modellt letöltött. Ha törlöd ezt a fájlt, a program újra ellenőrzi őket.\n")
    print("  kész.", flush=True)


if __name__ == "__main__":
    main()
