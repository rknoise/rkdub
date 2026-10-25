# -*- coding: utf-8 -*-
"""A magyar sorok elhelyezése az idővonalon, hangerő-illesztés, keverés a háttérrel."""

import numpy as np

from .media import MIX_SR


def _active_rms(y, sr, floor_db=-45.0):
    """A hangos (beszédes) részek átlagos hangereje; a csendet nem számolja bele."""
    if y.ndim > 1:
        y = y.mean(axis=1)
    hop = int(0.05 * sr)
    if len(y) < hop:
        return float(np.sqrt(np.mean(y ** 2) + 1e-12))
    frames = y[: len(y) // hop * hop].reshape(-1, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    loud = rms[20 * np.log10(rms) > floor_db]
    return float(np.sqrt(np.mean(loud ** 2))) if len(loud) else float(np.sqrt(np.mean(rms ** 2)))


def match_loudness(y, ref, sr_y, sr_ref):
    g = _active_rms(ref, sr_ref) / max(1e-6, _active_rms(y, sr_y))
    return y * float(np.clip(g, 0.25, 4.0))


def time_stretch(y, sr, rate):
    """Hangmagasság-tartó gyorsítás (rate > 1: rövidebb), csak kis mértékben használjuk."""
    if abs(rate - 1.0) < 0.01:
        return y
    import librosa

    return librosa.effects.time_stretch(y, rate=rate, n_fft=1024, hop_length=256).astype(np.float32)


def fade(y, sr, ms=8):
    n = min(len(y) // 2, int(sr * ms / 1000))
    if n > 0:
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        y = y.copy()
        y[:n] *= ramp
        y[-n:] *= ramp[::-1]
    return y


def trim_silence(y, sr, db=-40.0):
    """Az elejéről és végéről a csend levágása (a beszéd a sor elején induljon)."""
    hop = int(0.01 * sr)
    if len(y) < hop * 3:
        return y
    frames = y[: len(y) // hop * hop].reshape(-1, hop)
    loud = np.where(20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9) > db)[0]
    if not len(loud):
        return y
    a = max(0, (loud[0] - 2) * hop)
    b = min(len(y), (loud[-1] + 3) * hop)
    return y[a:b]


def place_lines(items, total_len, sr=MIX_SR, max_drift=0.8, gap=0.06):
    """items: [{"start", "end", "y" (mono, sr)}] időrendben. Ha egy sor túlnyúlik, a következő
    kicsit később indul (legfeljebb max_drift mp-cel); ha ez sem elég, azt a sort kissé gyorsítja.
    -> (mono sáv, [(tényleges kezdés, vég)])"""
    track = np.zeros(total_len, dtype=np.float32)
    placed, prev_end = [], 0.0
    for it in items:
        y = it["y"]
        t0 = max(it["start"], prev_end + gap)
        drift = t0 - it["start"]
        if drift > max_drift:
            t0 = it["start"] + max_drift
        # a következő sor kezdetéig (vagy a fájl végéig) kell végeznie, ha lehet
        limit = it.get("hard_end")
        if limit is not None:
            room = limit - t0
            dur = len(y) / sr
            if dur > room > 0.3:
                rate = min(1.25, dur / room)
                y = time_stretch(y, sr, rate)
        a = int(round(t0 * sr))
        b = min(total_len, a + len(y))
        if b > a:
            track[a:b] += fade(y[: b - a], sr)
        placed.append((t0, t0 + len(y) / sr))
        prev_end = t0 + len(y) / sr
    return track, placed


def duck_original(vocals, spans, sr=MIX_SR, pad_before=0.08, pad_after=0.15, fade_s=0.05):
    """Az eredeti beszédsáv elnémítása a lefordított sorok alatt; máshol (nevetés, kiáltás,
    sóhaj) megmarad. -> a megmaradó eredeti hang"""
    gain = np.ones(len(vocals), dtype=np.float32)
    nf = int(fade_s * sr)
    ramp = np.linspace(1, 0, nf, dtype=np.float32) if nf > 0 else None
    for s, e in spans:
        a = max(0, int((s - pad_before) * sr))
        b = min(len(gain), int((e + pad_after) * sr))
        gain[a:b] = 0.0
        if ramp is not None:
            fa = max(0, a - nf)
            gain[fa:a] = np.minimum(gain[fa:a], ramp[nf - (a - fa):])
            fb = min(len(gain), b + nf)
            gain[b:fb] = np.minimum(gain[b:fb], ramp[::-1][: fb - b])
    return vocals * (gain[:, None] if vocals.ndim > 1 else gain)


def limiter(y, sr, ceiling=0.97, release_s=0.25):
    """Egyszerű csúcskorlátozó: a túl hangos pillanatokat halkítja, a többihez nem nyúl."""
    peak = np.abs(y).max(axis=1) if y.ndim > 1 else np.abs(y)
    if peak.max() <= ceiling:
        return y
    need = np.minimum(1.0, ceiling / np.maximum(peak, 1e-9))
    # a halkítás gyorsan jön, lassan enged el (blokkonként, hogy gyors legyen)
    blk = max(1, int(0.005 * sr))
    n = len(need) // blk * blk
    g = need[:n].reshape(-1, blk).min(axis=1)
    rel = np.exp(-blk / (release_s * sr))
    out = np.empty_like(g)
    cur = 1.0
    for i, v in enumerate(g):
        cur = v if v < cur else v + (cur - v) * rel
        out[i] = cur
    gain = np.repeat(out, blk)
    gain = np.concatenate([gain, np.full(len(need) - n, gain[-1] if len(gain) else 1.0)])
    # előretekintés: a halkítás kicsit korábban kezdődjön
    look = blk * 2
    gain = np.minimum(gain, np.concatenate([gain[look:], np.full(look, gain[-1])]))
    return y * (gain[:, None] if y.ndim > 1 else gain)


def to_srt(lines, path, key="hu", times=None):
    def ts(t):
        t = max(0.0, t)
        h, r = divmod(int(t * 1000), 3600_000)
        m, r = divmod(r, 60_000)
        s, ms = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(path, "w", encoding="utf-8") as f:
        n = 0
        for i, ln in enumerate(lines):
            text = (ln.get(key) or "").strip()
            if not text:
                continue
            s, e = times[i] if times else (ln["start"], ln["end"])
            n += 1
            f.write(f"{n}\n{ts(s)} --> {ts(max(e, s + 0.5))}\n{text}\n\n")
