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


def ensure_ffmpeg_on_path():
    """Az audio-separator (és a pydub) a PATH-on keresi az "ffmpeg" parancsot, és nélküle leáll ("FFmpeg is not
    installed"). Ha a gépen nincs ffmpeg, az imageio-ffmpeg csomagét ffmpeg(.exe) néven a .tools\\ffmpeg mappába
    másoljuk, és azt tesszük a PATH elejére. Az audio_separator importálása ELŐTT kell hívni."""
    if shutil.which("ffmpeg"):
        return
    import imageio_ffmpeg

    src = imageio_ffmpeg.get_ffmpeg_exe()
    folder = os.path.join(ROOT, ".tools", "ffmpeg")
    dst = os.path.join(folder, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not (os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src)):
        os.makedirs(folder, exist_ok=True)
        tmp = dst + ".tmp"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        if os.name != "nt":
            os.chmod(dst, 0o755)
    os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


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


def apply_cpu_override():
    """RKDUB_CPU=1: processzoron fut akkor is, ha van NVIDIA-kártya (a processzoros mód kipróbálásához).
    A torch importja ELŐTT kell hívni; a feladatot futtató gyerekfolyamat örökli."""
    if os.environ.get("RKDUB_CPU") == "1":
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"      # az üres érték Windowson törölné a változót


_cpu_mode = None


def cpu_mode():
    """Nincs használható NVIDIA-kártya (vagy RKDUB_CPU=1): a szinkron processzoron, könnyített beállításokkal
    fut. Az nvidia-smi alapján dönt, a torch betöltése nélkül (a felületnek is gyorsan kell)."""
    global _cpu_mode
    if _cpu_mode is None:
        _cpu_mode = True
        exe = shutil.which("nvidia-smi")
        if exe and os.environ.get("RKDUB_CPU") != "1":
            try:
                out = subprocess.run([exe, "-L"], capture_output=True, text=True, timeout=10,
                                     creationflags=NO_WINDOW).stdout
                _cpu_mode = "GPU" not in out
            except Exception:
                pass
    return _cpu_mode


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
