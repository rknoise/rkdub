# -*- coding: utf-8 -*-
"""Beszélők szétválasztása soronként: hangszín (ECAPA hangnyomat) ÉS hangmagasság együtt, majd csoportosítás.
Ebből lesz a beszélőnkénti hangminta, amivel minden szereplő végig ugyanazon a hangon szólal meg.

Miért a kettő együtt: rövid sorokon (főleg gépi, pl. AI-generált videóknál) a hangnyomat egyedül sokszor
összekeveri a szereplőket, a hangmagasság viszont (pl. idősebb férfi ~110 Hz, fiatal ~140 Hz) jól elválasztja
őket; a hangmagasság egyedül viszont egy kiabáló mondatnál tévedne. Ha így is rossz a beosztás, a felületen
soronként átírható a beszélő."""

import numpy as np

from .util import ROOT, free_gpu

ECAPA_MODEL = "speechbrain/spkrec-ecapa-voxceleb"
SV_MODEL = "microsoft/wavlm-base-plus-sv"      # tartalék, ha a speechbrain nincs telepítve
# A hangmagasság-különbség (log-arány) súlya és felső korlátja a távolságban. Automatikus módban kicsi, mert
# ugyanaz a beszélő is beszél hol mélyebben, hol magasabban (ezt ne vegye új szereplőnek); ha a beszélők számát
# megadják, nagy, mert akkor a feladat csak a szétosztás, és ott a hangmagasság a legmegbízhatóbb jel.
PITCH_AUTO = (0.5, 0.15)
PITCH_FIXED = (1.5, 0.45)
AUTO_THRESHOLD = 0.5       # automatikus módban e fölötti átlagos távolság már külön beszélő


class SpeakerEmbedder:
    """Hangnyomat 16 kHz-es mono hangból: ECAPA-TDNN (speechbrain), ha elérhető, különben WavLM x-vektor."""

    def __init__(self):
        import os

        import torch

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            from speechbrain.inference.speaker import EncoderClassifier

            self.kind = "ecapa"
            self.model = EncoderClassifier.from_hparams(
                source=ECAPA_MODEL, savedir=os.path.join(ROOT, ".models", "ecapa"),
                run_opts={"device": "cuda:0" if self.device == "cuda" else "cpu"})
        except Exception:
            from transformers import AutoFeatureExtractor, WavLMForXVector

            self.kind = "wavlm"
            self.fe = AutoFeatureExtractor.from_pretrained(SV_MODEL)
            self.model = WavLMForXVector.from_pretrained(SV_MODEL).to(self.device).eval()

    def _one(self, c):
        torch = self.torch
        with torch.no_grad():
            if self.kind == "ecapa":
                t = torch.from_numpy(np.ascontiguousarray(c, dtype=np.float32)).unsqueeze(0).to(self.device)
                return self.model.encode_batch(t).squeeze().float().cpu().numpy()
            inp = self.fe(c, sampling_rate=16000, return_tensors="pt").to(self.device)
            return self.model(**inp).embeddings[0].float().cpu().numpy()

    def embed(self, y16):
        """-> normalizált hangnyomat. A hosszú részt 8 mp-es darabok átlagaként."""
        chunks = [y16[i:i + 8 * 16000] for i in range(0, max(1, len(y16) - 8000), 8 * 16000)] or [y16]
        embs = []
        for c in chunks:
            if len(c) < 8000:
                c = np.pad(c, (0, 8000 - len(c)))
            e = self._one(c)
            embs.append(e / (np.linalg.norm(e) + 1e-9))
        e = np.mean(embs, axis=0)
        return e / (np.linalg.norm(e) + 1e-9)

    def close(self):
        self.model = None
        free_gpu()


def median_pitch(y16):
    """A hangos részek medián alaphangja (Hz), vagy None, ha nincs zöngés rész."""
    import librosa

    if len(y16) < 4000:
        return None
    y = y16[: 8 * 16000]          # 8 mp bőven elég, és a pyin így gyors marad
    f0, voiced, _ = librosa.pyin(y, fmin=60, fmax=400, sr=16000, frame_length=1024, hop_length=320)
    vals = f0[voiced & ~np.isnan(f0)] if voiced is not None else f0[~np.isnan(f0)]
    return float(np.median(vals)) if len(vals) >= 5 else None


FEMALE_HZ = 165.0          # e fölötti medián alaphang: női hang (férfi ~85-155 Hz, női ~165-255 Hz)


def _cluster(D, k):
    from sklearn.cluster import AgglomerativeClustering

    if len(D) == 1:
        return np.zeros(1, int)
    if k:
        return AgglomerativeClustering(n_clusters=min(k, len(D)), metric="precomputed",
                                       linkage="average").fit_predict(D)
    return AgglomerativeClustering(n_clusters=None, metric="precomputed", linkage="average",
                                   distance_threshold=AUTO_THRESHOLD).fit_predict(D)


def assign_speakers(lines, vocals_16k, num_speakers=0, log=print, genders=None):
    """Minden sorhoz "speaker" (0, 1, ...). num_speakers=0: automatikus.
    genders: a szereplők neme, ha ismert (pl. ["f", "f", "m", "m", "m"] = 2 nő, 3 férfi; a többi szereplőé
    ismeretlen). Ilyenkor a sorok előbb hangmagasság szerint nőkre és férfiakra oszlanak, és csak a csoporton
    belül bomlanak szereplőkre; a sorszámok a megadott nemekhez igazodnak."""
    if not lines:
        return lines
    genders = [g for g in (genders or []) if g in ("f", "m")][: num_speakers or None]
    if num_speakers == 1 or len(lines) == 1:
        for ln in lines:
            ln["speaker"] = 0
        log("Beszélők: 1 (beállítás szerint)" if num_speakers == 1 else "Beszélők: 1")
        return lines
    emb = SpeakerEmbedder()
    vecs, pitch = [], []
    for ln in lines:
        seg = vocals_16k[int(ln["start"] * 16000): int(ln["end"] * 16000)]
        vecs.append(emb.embed(seg))
        pitch.append(median_pitch(seg))
    kind = emb.kind
    emb.close()
    X = np.stack(vecs)
    n = len(lines)
    # távolság: hangszín (1 - koszinusz-hasonlóság, a WavLM szűkebb skálája felnagyítva) + hangmagasság
    timbre = 1.0 - X @ X.T
    if kind == "wavlm":
        timbre = timbre * 3.0
    weight, cap = PITCH_FIXED if num_speakers and num_speakers > 1 else PITCH_AUTO
    pd = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if pitch[i] and pitch[j]:
                pd[i, j] = min(cap, weight * abs(np.log(pitch[i] / pitch[j])))
    D = np.clip(0.5 * timbre + pd, 0.0, None)
    np.fill_diagonal(D, 0.0)

    nf, nm = genders.count("f"), genders.count("m")
    rest = (num_speakers - nf - nm) if num_speakers else 0
    sex = ["f" if p and p >= FEMALE_HZ else "m" if p else None for p in pitch]
    if nf and nm and rest == 0:
        # minden szereplő neme ismert: nők és férfiak külön csoportosítva, a saját létszámukra
        labels = np.full(n, -1)
        nxt = 0
        for g, k in (("f", nf), ("m", nm)):
            idx = [i for i in range(n) if sex[i] == g]
            if not idx:
                continue
            sub = _cluster(D[np.ix_(idx, idx)], k)
            for i, l in zip(idx, sub):
                labels[i] = nxt + int(l)
            nxt += int(sub.max()) + 1
        for i in range(n):                    # hangmagasság nélküli sor: a hozzá leghasonlóbb csoportba
            if labels[i] < 0:
                cands = {int(l): D[i, labels == l].mean() for l in set(labels.tolist()) if l >= 0}
                labels[i] = min(cands, key=cands.get) if cands else 0
    else:
        if (nf or nm) and len({x for x in sex if x}) > 1:
            # ismert nemek mellett a női és férfi sorok ne kerüljenek egy szereplőhöz
            for i in range(n):
                for j in range(n):
                    if sex[i] and sex[j] and sex[i] != sex[j] and (nf and nm or rest):
                        D[i, j] += 1.0
        labels = _cluster(D, num_speakers if num_speakers and num_speakers > 1 else 0)

    # a csoportok sorszáma: a megadott nemek sorrendjében (pl. B1-B2 nő, B3-B5 férfi), azon belül a
    # többet beszélő előre; ismeretlen nemű helyek a végén
    talk = {c: sum(ln["end"] - ln["start"] for ln, l in zip(lines, labels) if l == c) for c in set(labels.tolist())}
    csex = {}
    for c in talk:
        ps = [p for p, l in zip(pitch, labels) if l == c and p]
        csex[c] = ("f" if np.median(ps) >= FEMALE_HZ else "m") if ps else None
    slots = genders + ["?"] * max(0, (num_speakers or 0) - len(genders))
    free = list(range(len(slots)))
    order = {}
    for c in sorted(talk, key=lambda c: -talk[c]):
        pick = next((s_ for s_ in free if slots[s_] == csex[c]), None)
        if pick is None:
            pick = next((s_ for s_ in free if slots[s_] == "?"), None)
        if pick is None and free:
            pick = free[0]
        if pick is None:
            pick = len(slots) + len(order)
        else:
            free.remove(pick)
        order[c] = pick
    # sorszám-hézagok nélkül (ha egy megadott szereplőnek nem jutott sor)
    remap = {v: k for k, v in enumerate(sorted(order.values()))}
    for ln, l in zip(lines, labels):
        ln["speaker"] = int(remap[order[int(l)]])
    info = []
    for c in sorted(order, key=lambda c: order[c]):
        ps = [p for p, l in zip(pitch, labels) if l == c and p]
        g = {"f": "nő", "m": "férfi"}.get(csex[c], "?")
        info.append(f"{remap[order[c]] + 1}. beszélő ({g}) {talk[c]:.0f} mp" + (f", ~{np.median(ps):.0f} Hz" if ps else ""))
    log(f"Beszélők: {len(order)} (" + "; ".join(info) + ")")
    return lines
