# -*- coding: utf-8 -*-
"""Beszéd és háttér (zene, zajok) szétválasztása: BS-Roformer (audio-separator, GPU)."""

import logging
import os
import shutil

from .util import ROOT, free_gpu

SEP_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"   # BS-Roformer-Viperx-1297
MODEL_DIR = os.path.join(ROOT, ".models", "separator")


def separate(mix_wav, out_dir, log=print):
    """-> (vocals.wav, background.wav) az out_dir-ben, a bemenettel azonos mintavétellel."""
    from audio_separator.separator import Separator

    os.makedirs(MODEL_DIR, exist_ok=True)
    tmp = os.path.join(out_dir, "_sep")
    os.makedirs(tmp, exist_ok=True)
    log("Beszéd és háttér szétválasztása (BS-Roformer)...")
    sep = Separator(output_dir=tmp, output_format="WAV", model_file_dir=MODEL_DIR,
                    log_level=logging.WARNING, use_autocast=True)
    sep.load_model(model_filename=SEP_MODEL)
    files = sep.separate(mix_wav, {"Vocals": "vocals", "Instrumental": "background"})
    del sep
    free_gpu()
    vocals, background = os.path.join(out_dir, "vocals.wav"), os.path.join(out_dir, "background.wav")
    for f in files:
        p = f if os.path.isabs(f) else os.path.join(tmp, f)
        name = os.path.basename(p).lower()
        if name.startswith("vocals"):
            shutil.move(p, vocals)
        elif name.startswith("background"):
            shutil.move(p, background)
    shutil.rmtree(tmp, ignore_errors=True)
    if not (os.path.exists(vocals) and os.path.exists(background)):
        raise RuntimeError(f"a szétválasztás nem adott két sávot: {files}")
    return vocals, background
