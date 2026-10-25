# -*- coding: utf-8 -*-
"""Bemenet vizsgálata, hang kibontása, végső videó/hang összeállítása (ffmpeg)."""

import os

import numpy as np

from .util import AUDIO_EXT, VIDEO_EXT, run_ffmpeg

MIX_SR = 44100   # a keverés mintavétele (a hangleválasztó modell is ezt várja)


def probe(path):
    """{"duration", "has_video", "has_audio", "vcodec", "acodec"} a PyAV-vel (a faster-whisper hozza)."""
    import av

    info = {"duration": 0.0, "has_video": False, "has_audio": False, "vcodec": "", "acodec": ""}
    with av.open(path) as c:
        if c.duration:
            info["duration"] = c.duration / 1_000_000
        for s in c.streams:
            if s.type == "video" and not info["has_video"]:
                if _is_cover(s):   # az MP3/M4A borítóképe nem videó
                    continue
                info["has_video"], info["vcodec"] = True, s.codec_context.name
            elif s.type == "audio" and not info["has_audio"]:
                info["has_audio"], info["acodec"] = True, s.codec_context.name
                if not info["duration"] and s.duration and s.time_base:
                    info["duration"] = float(s.duration * s.time_base)
    if os.path.splitext(path)[1].lower() in AUDIO_EXT:
        info["has_video"] = False
    return info


def _is_cover(stream):
    try:
        import av

        return bool(stream.disposition & av.stream.Disposition.attached_pic)
    except Exception:
        return False


def is_media(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in VIDEO_EXT or ext in AUDIO_EXT


def extract_audio(src, dst, sr=MIX_SR, channels=2):
    run_ffmpeg(["-i", src, "-vn", "-sn", "-dn", "-ac", str(channels), "-ar", str(sr), "-c:a", "pcm_f32le", dst],
               "A hang kibontása")


def read_audio(path, sr=None, mono=False):
    """float32, (n,) vagy (n, ch). Ha sr meg van adva és eltér, átmintavételez."""
    import soundfile as sf

    y, s = sf.read(path, dtype="float32", always_2d=True)
    if mono:
        y = y.mean(axis=1)
    if sr and s != sr:
        import librosa

        if y.ndim == 1:
            y = librosa.resample(y, orig_sr=s, target_sr=sr, res_type="soxr_hq")
        else:
            y = librosa.resample(y.T, orig_sr=s, target_sr=sr, res_type="soxr_hq").T
        s = sr
    return np.ascontiguousarray(y, dtype=np.float32), s


def write_audio(path, y, sr):
    import soundfile as sf

    sf.write(path, y, sr, subtype="FLOAT" if path.lower().endswith(".wav") else None)


def mux_video(src, dub_wav, out_path, keep_original=True, srt_path=None):
    """A videókép változatlanul (újrakódolás nélkül), mellé a magyar hang (1. sáv, alapértelmezett),
    kérésre az eredeti hang (2. sáv) és a magyar felirat (kikapcsolható)."""
    ext = os.path.splitext(out_path)[1].lower()
    args = ["-i", src, "-i", dub_wav]
    if srt_path:
        args += ["-i", srt_path]
    args += ["-map", "0:v:0", "-map", "1:a:0"]
    if keep_original:
        args += ["-map", "0:a:0?"]
    if srt_path:
        args += ["-map", "2:s:0"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    if srt_path:
        args += ["-c:s", "mov_text" if ext in (".mp4", ".m4v", ".mov") else "srt"]
    args += ["-metadata:s:a:0", "language=hun", "-metadata:s:a:0", "title=Magyar szinkron",
             "-disposition:a:0", "default"]
    if keep_original:
        args += ["-metadata:s:a:1", "title=Eredeti", "-disposition:a:1", "0"]
    if srt_path:
        args += ["-metadata:s:s:0", "language=hun", "-metadata:s:s:0", "title=Magyar",
                 "-disposition:s:0", "0"]
    if ext in (".mp4", ".m4v", ".mov"):
        args += ["-movflags", "+faststart"]
    args += [out_path]
    run_ffmpeg(args, "A videó összeállítása")


def encode_audio(dub_wav, out_path):
    ext = os.path.splitext(out_path)[1].lower()
    codec = {".mp3": ["-c:a", "libmp3lame", "-q:a", "2"], ".m4a": ["-c:a", "aac", "-b:a", "192k"],
             ".flac": ["-c:a", "flac"], ".wav": ["-c:a", "pcm_s16le"]}.get(ext, ["-c:a", "aac", "-b:a", "192k"])
    run_ffmpeg(["-i", dub_wav, *codec, out_path], "A hangfájl mentése")


def output_ext(src, has_video):
    ext = os.path.splitext(src)[1].lower()
    if has_video:
        return ".mkv" if ext in (".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts", ".mpg", ".mpeg") else ".mp4"
    return ".mp3" if ext == ".mp3" else ".m4a"
