# -*- coding: utf-8 -*-
"""A szinkronszöveg exportálása egy egyszerű szövegfájlba (amit ember vagy AI, pl. Claude, át tud nézni
és javítani), és a javított változat visszaolvasása. Csak a megváltozott sorok frissülnek."""

import os
import re

from .translate import max_chars
from .util import OUT_DIR, fmt_time

HEADER = """# rkdub – magyar szinkronszöveg: {name}
# (Magyarul: a HU sorokat javítsd, a többit hagyd békén. A kész fájlt az rkdub Importálás gombjával töltheted vissza.)
#
# INSTRUCTIONS FOR THE REVIEWER (human or AI assistant):
# This is the script of a Hungarian dub of an {lang} video. Each block below is one line of dialogue.
#   EN:  the original line (from speech recognition, may contain recognition errors; do not edit it).
#   HU:  the Hungarian dub line, also used as the subtitle. Improve it so it sounds like a professional
#        Hungarian dub: natural, idiomatic spoken Hungarian (no word-for-word translation, no anglicisms
#        such as "Ez egy...", "-val való", "számára", "gaming"), perfect grammar, the same meaning and tone.
#        Keep names, brands and product names. The block header shows the time slot and a character
#        budget ("max"): stay close to it so the line can be spoken in time.
#   SAY: optional. How the voice should pronounce the line when it contains names, acronyms or foreign
#        words: write those in Hungarian spelling (Xbox -> ikszboksz, AMD -> á-em-dé, Jack -> dzsek).
#        Keep numbers as digits (they are spelled out automatically). Leave SAY empty if not needed.
# Keep every "### <number>" line and the "EN:", "HU:", "SAY:" labels. Do not merge, split, reorder or
# renumber the blocks. Return the complete file as plain text.
"""

_LANG = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "ja": "Japanese"}


def export_path(job):
    from .pipeline import safe_stem

    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, safe_stem(job.src) + "_forditas.txt")


def export_script(job, lang="en"):
    """-> (fájl útvonala, szöveg)"""
    from .pipeline import safe_stem

    lines = job.lines()
    out = [HEADER.format(name=safe_stem(job.src), lang=_LANG.get(lang, lang or "English"))]
    for ln in lines:
        dur = ln["end"] - ln["start"]
        out.append(f"### {ln['id'] + 1} | {fmt_time(ln['start'])} – {fmt_time(ln['end'])} | {dur:.1f} s | "
                   f"max {max_chars(ln)} chars | speaker {ln.get('speaker', 0) + 1}")
        out.append(f"EN: {ln['en']}")
        out.append(f"HU: {ln.get('hu', '')}")
        out.append(f"SAY: {ln.get('say', '')}")
        out.append("")
    text = "\n".join(out)
    path = export_path(job)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path, text


_HEAD = re.compile(r"^\s*#{2,4}\s*(\d+)\b")
_LABEL = re.compile(r"^\s*[*_]*\s*(EN|HU|SAY)\s*[*_]*\s*[:：]\s*[*_]*\s?(.*?)\s*[*_]*\s*$", re.I)


def parse_script(text):
    """{sorszám (1-től): {"en", "hu", "say"}} a beolvasott szövegből. Elnézi a csevegőprogramok szokásos
    "díszítéseit" is (```-blokk, **HU:**)."""
    blocks, cur, field = {}, None, None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            continue
        m = _HEAD.match(line)
        if m:
            cur, field = blocks.setdefault(int(m.group(1)), {}), None
            continue
        if cur is None:
            continue
        m = _LABEL.match(line)
        if m:
            field = m.group(1).lower()
            cur[field] = m.group(2).strip()
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            field = None
            continue
        if field in ("hu", "say"):            # több sorba tört szöveg folytatása
            cur[field] = (cur.get(field, "") + " " + line.strip()).strip()
    return blocks


def apply_script(job, text):
    """A javított szöveg alkalmazása. -> {"changed", "say", "unknown", "missing"}"""
    from .translate import bad_script, clean_hu

    blocks = parse_script(text)
    if not blocks:
        raise ValueError("nem találtam benne egyetlen „### szám” blokkot sem")
    lines = job.lines()
    by_no = {ln["id"] + 1: ln for ln in lines}
    stats = {"changed": 0, "say": 0, "unknown": [], "missing": 0}
    for no, b in sorted(blocks.items()):
        ln = by_no.get(no)
        if ln is None:
            stats["unknown"].append(no)
            continue
        hu = clean_hu(b.get("hu", "") or "")
        say = clean_hu(b.get("say", "") or "")
        if hu and hu != ln.get("hu") and not bad_script(hu):
            ln.setdefault("hu_draft", ln.get("hu", ""))
            ln["hu"], ln["edited"] = hu, True
            if not say:                         # a régi kiejtés már nem érvényes
                ln.pop("say", None)
                ln.pop("say_edited", None)
            stats["changed"] += 1
        if say and say != ln.get("say") and not bad_script(say):
            ln["say"], ln["say_edited"] = say, True
            stats["say"] += 1
    stats["missing"] = len([no for no in by_no if no not in blocks])
    job.save_lines(lines)
    return stats
