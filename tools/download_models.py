# -*- coding: utf-8 -*-
"""A telepítő hívja: előre letölti a modelleket, hogy az első szinkron ne várjon rájuk."""

import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from huggingface_hub import snapshot_download

    print("  OmniVoice (magyar hang)...", flush=True)
    snapshot_download("k2-fsa/OmniVoice")
    print("  Whisper large-v3-turbo (beszédfelismerés)...", flush=True)
    from faster_whisper.utils import download_model

    download_model("large-v3-turbo")
    print("  Whisper large-v3 (a kész magyar sorok ellenőrzése)...", flush=True)
    download_model("large-v3")
    print("  WavLM és ECAPA (beszélők azonosítása)...", flush=True)
    snapshot_download("microsoft/wavlm-base-plus-sv")
    from dub.speakers import SpeakerEmbedder

    SpeakerEmbedder().close()              # az ECAPA-modellt a .models\ecapa mappába tölti
    print("  BS-Roformer (beszéd és háttér szétválasztása)...", flush=True)
    import logging

    from audio_separator.separator import Separator

    from dub.separate import MODEL_DIR, SEP_MODEL

    os.makedirs(MODEL_DIR, exist_ok=True)
    sep = Separator(model_file_dir=MODEL_DIR, log_level=logging.WARNING, output_dir=MODEL_DIR)
    sep.download_model_files(SEP_MODEL) if hasattr(sep, "download_model_files") else sep.load_model(SEP_MODEL)
    sep.list_supported_model_files()      # a modellista is helyben legyen (internet nélküli futáshoz)

    from dub.util import MODELS_OK

    with open(MODELS_OK, "w", encoding="utf-8") as f:   # innentől a program nem kérdezi le az internetet
        f.write("A telepítő minden modellt letöltött. Ha törlöd ezt a fájlt, a program újra ellenőrzi őket.\n")
    print("  kész.", flush=True)


if __name__ == "__main__":
    main()
