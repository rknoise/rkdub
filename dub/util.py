# -*- coding: utf-8 -*-
"""Közös segédek: mappák, ffmpeg, JSON, GPU-memória, naplózás."""

import gc
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_DIR = os.path.join(ROOT, "munkak")      # lépésenkénti köztes fájlok (folytatható, újragenerálható)
OUT_DIR = os.path.join(ROOT, "kesz")         # a kész, szinkronizált fájlok
HOME_DIR = os.path.join(os.path.expanduser("~"), ".rkdub")
SETTINGS_PATH = os.path.join(HOME_DIR, "settings.json")
RKSUBBY_DEEPL_KEY = os.path.join(os.path.expanduser("~"), ".rksubby", "deepl_key.txt")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".ts", ".mpg", ".mpeg"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


MODELS_OK = os.path.join(ROOT, ".models", "letoltve.txt")   # a telepítő írja, ha minden modell megvan


def use_offline_models():
    """Ha a telepítő már letöltött minden modellt, a Hugging Face-t nem kérdezzük le minden indításnál
    (gyorsabb, és internet nélkül is működik). A huggingface_hub importja ELŐTT kell hívni."""
    if os.path.exists(MODELS_OK) and os.environ.get("RKDUB_ONLINE") != "1":
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        return True
    return False


def is_offline_error(e):
    s = f"{type(e).__name__} {e}".lower()
    return any(k in s for k in ("offline", "localentrynotfound", "local_files_only", "cannot find the requested files"))


def ffmpeg_exe():
    """A PATH-on lévő ffmpeg, különben az imageio-ffmpeg csomagé."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args, what="ffmpeg"):
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"{what} nem sikerült: {p.stderr.strip()[-800:]}")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_settings():
    return read_json(SETTINGS_PATH, {}) or {}


def save_settings(data):
    os.makedirs(HOME_DIR, exist_ok=True)
    write_json(SETTINGS_PATH, data)


def free_gpu():
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def fmt_time(t):
    t = max(0.0, float(t))
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def short_err(e):
    s = str(e).strip().splitlines()
    return f"{type(e).__name__}: {s[-1] if s else ''}"[:400]


def log_to_console(msg):
    try:
        print(msg, flush=True)
    except Exception:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
