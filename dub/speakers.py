# -*- coding: utf-8 -*-
"""Beszélők szétválasztása soronként: hangszín (ECAPA hangnyomat) és egy kis súlyú hangmagasság-tag alapján.
Ebből lesz a beszélőnkénti hangminta, amivel minden szereplő végig ugyanazon a hangon szólal meg.

Két lépésben: előbb csak a megbízható (hosszabb) sorok csoportosulnak, aztán minden sor a hozzá leghasonlóbb
szereplőhöz kerül, és ez ismétlődik, amíg a szereplők átlagos hangnyomata már nem változik. Az egyes sorok
hangnyomata zajos (rövid, érzelmes, kiabáló mondat), a szereplők átlaga viszont stabil, ezért a döntés az
átlagokhoz mér. Automatikus módban a program előbb bőven szétválogat, aztán összevonja, ami az átlag szerint
ugyanaz az ember; megadott szereplőszámnál is összevonja a nyilvánvalóan azonos embert (ha túl sokat adtak meg).

Mérve (2026-11-04): 4 szereplős mérőpárbeszéd, 4 szereplő beállítva: 68% -> 99% helyes hang; ugyanez 5-6-ra
állítva 89% -> 96-99%; a régi, erős hangmagasság-súly a két hasonló magasságú férfit összevonta, a kiabáló
nőt pedig kettévágta. Ha így is rossz a beosztás, a felületen soronként átírható a beszélő."""

import numpy as np

from .util import ROOT, free_gpu

ECAPA_MODEL = "speechbrain/spkrec-ecapa-voxceleb"
SV_MODEL = "microsoft/wavlm-base-plus-sv"      # tartalék, ha a speechbrain nincs telepítve
PITCH_W, PITCH_CAP = 0.2, 0.12   # hangmagasság-különbség (log-arány) súlya és felső korlátja: csak finomít
RELIABLE_SEC = 1.5               # az első csoportosításhoz ennél hosszabb sorok (a rövidek hangnyomata zajos)
OVER_THRESHOLD = 0.4             # automatikus mód: előbb bőven szétválogat (átlagos távolság e fölött külön) ...
MERGE_SIM = 0.5                  # ... aztán összevonja, aminek az átlagos hangnyomata ennyire hasonlít
SAME_PERSON_SIM = 0.6            # megadott szereplőszámnál ennyire hasonló két csoport biztosan ugyanaz az ember
TINY_SHARE, TINY_SEC, TINY_SIM = 0.02, 8.0, 0.3   # automatikus mód: a beszéd <2%-a és <8 mp csoport beolvad
SHORT_ONLY_SEC = 1.0             # (ha elég hasonló), a csupa ennél rövidebb sorból álló csoport mindenképp
REFINE_ITERS = 10
FEMALE_HZ = 165.0                # e fölötti medián alaphang: női hang (férfi ~85-155 Hz, női ~165-255 Hz)


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


# --- csoportosítás -------------------------------------------------------------------------------------------

def _pitch_term(pa, pb):
    if not pa or not pb:
        return 0.0
    return min(PITCH_CAP, PITCH_W * abs(np.log(pa / pb)))


def _centroids(X, pitch, dur, labels):
    """Csoportonként a sorok hangnyomatának (hosszal súlyozott) átlaga és a medián hangmagasság."""
    ks = sorted(set(labels.tolist()))
    C, cp = [], []
    for k in ks:
        idx = np.where(labels == k)[0]
        c = (X[idx] * np.minimum(dur[idx], 6.0)[:, None]).sum(0)
        C.append(c / (np.linalg.norm(c) + 1e-9))
        ps = [pitch[i] for i in idx if pitch[i]]
        cp.append(float(np.median(ps)) if ps else None)
    return ks, np.stack(C), cp


def _nearest(X, pitch, C, cp, ks):
    D = 0.5 * (1.0 - X @ C.T)
    for i in range(len(X)):
        for j in range(len(ks)):
            D[i, j] += _pitch_term(pitch[i], cp[j])
    return np.array(ks)[D.argmin(1)]


def _refine(X, pitch, dur, labels):
    """Minden sor a legközelebbi szereplő-átlaghoz, amíg a beosztás már nem változik."""
    for _ in range(REFINE_ITERS):
        ks, C, cp = _centroids(X, pitch, dur, labels)
        new = _nearest(X, pitch, C, cp, ks)
        if (new == labels).all():
            break
        labels = new
    return labels


def _merge_similar(X, pitch, dur, labels, thr):
    """A két leghasonlóbb csoport összevonása, amíg az átlagos hangnyomatuk hasonlósága legalább thr."""
    while True:
        ks, C, _cp = _centroids(X, pitch, dur, labels)
        if len(ks) < 2:
            return labels
        S = C @ C.T
        np.fill_diagonal(S, -1.0)
        i, j = np.unravel_index(int(np.argmax(S)), S.shape)
        if S[i, j] < thr:
            return labels
        keep, drop = (ks[i], ks[j]) if dur[labels == ks[i]].sum() >= dur[labels == ks[j]].sum() else (ks[j], ks[i])
        labels = labels.copy()
        labels[labels == drop] = keep
        labels = _refine(X, pitch, dur, labels)


def _merge_tiny(X, pitch, dur, labels):
    """Automatikus mód: a pár kiabálásból, nevetésből álló mini-csoport ne kapjon külön hangot, ha hasonlít
    egy nagyra (vagy csupa nagyon rövid sorból áll, amiknek a hangnyomata úgysem megbízható)."""
    total = dur.sum()
    kept = set()
    while True:
        ks, C, _cp = _centroids(X, pitch, dur, labels)
        talk = {c: dur[labels == c].sum() for c in ks}
        small = {c for c in ks if talk[c] < TINY_SHARE * total and talk[c] < TINY_SEC}
        cand = [c for c in ks if c in small and c not in kept]
        big = [c for c in ks if c not in small]
        if not cand or not big:
            return labels
        c = min(cand, key=lambda c: talk[c])
        sims = {b: float(C[ks.index(c)] @ C[ks.index(b)]) for b in big}
        b = max(sims, key=sims.get)
        if sims[b] < TINY_SIM and dur[labels == c].max() >= SHORT_ONLY_SEC:
            kept.add(c)                       # tényleg más hang (pl. egy rövid bejátszás)
        else:
            labels = labels.copy()
            labels[labels == c] = b


def _group(X, pitch, dur, k):
    """Egy sorhalmaz csoportosítása: k szereplőre (k > 1), vagy automatikusan (k == 0)."""
    n = len(X)
    if n == 1 or k == 1:
        return np.zeros(n, dtype=int)
    from sklearn.cluster import AgglomerativeClustering

    D = 0.5 * (1.0 - X @ X.T)
    for i in range(n):
        for j in range(n):
            D[i, j] += _pitch_term(pitch[i], pitch[j])
    np.fill_diagonal(D, 0.0)
    D = np.clip(D, 0.0, None)
    rel = np.where(dur >= RELIABLE_SEC)[0]
    if len(rel) < max(4, 2 * (k or 2)):
        rel = np.argsort(-dur)[: max(4, min(n, 2 * (k or 2)))]
    rel = np.sort(rel)
    if len(rel) < 2:
        return np.zeros(n, dtype=int)
    sub = D[np.ix_(rel, rel)]
    if k:
        lab_rel = AgglomerativeClustering(n_clusters=min(k, len(rel)), metric="precomputed",
                                          linkage="average").fit_predict(sub)
    else:
        lab_rel = AgglomerativeClustering(n_clusters=None, metric="precomputed", linkage="average",
                                          distance_threshold=OVER_THRESHOLD).fit_predict(sub)
    ks, C, cp = _centroids(X[rel], [pitch[i] for i in rel], dur[rel], lab_rel)
    labels = _nearest(X, pitch, C, cp, ks)
    labels[rel] = lab_rel
    labels = _refine(X, pitch, dur, labels)
    if k:
        return _merge_similar(X, pitch, dur, labels, SAME_PERSON_SIM)
    labels = _merge_similar(X, pitch, dur, labels, MERGE_SIM)
    labels = _merge_tiny(X, pitch, dur, labels)
    return _refine(X, pitch, dur, labels)


def _sexes(X, pitch, dur, labels):
    """Csoportonként a nem a csoport medián hangmagasságából (nem soronként: egy kiabáló férfi mondata
    180 Hz is lehet)."""
    ks, _C, cp = _centroids(X, pitch, dur, labels)
    return {c: ("f" if p >= FEMALE_HZ else "m") if p else None for c, p in zip(ks, cp)}


def assign_speakers(lines, vocals_16k, num_speakers=0, log=print, genders=None):
    """Minden sorhoz "speaker" (0, 1, ...) és "spk_sim" (mennyire hasonlít a szereplő átlagos hangjára; a
    hangminta a legjellemzőbb sorokból készül). num_speakers=0: automatikus.
    genders: a szereplők neme, ha ismert (pl. ["f", "f", "m", "m", "m"] = 2 nő, 3 férfi; a többi szereplőé
    ismeretlen). Ha minden szereplő neme meg van adva, a nők és a férfiak külön csoportosulnak; a sorszámok a
    megadott nemekhez igazodnak (B1-B2 nők, B3-B5 férfiak)."""
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
    emb.close()
    X = np.stack(vecs)
    dur = np.array([ln["end"] - ln["start"] for ln in lines])
    n = len(lines)
    k = num_speakers if num_speakers and num_speakers > 1 else 0

    nf, nm = genders.count("f"), genders.count("m")
    labels = None
    if k and nf and nm and nf + nm == k:
        # minden szereplő neme ismert: a sorok nemét a hangjuk csoportja adja (automatikus csoportosítással),
        # aztán a nők és a férfiak külön csoportosulnak a saját létszámukra
        auto = _group(X, pitch, dur, 0)
        sex = _sexes(X, pitch, dur, auto)
        line_sex = np.array([sex.get(int(a)) or "?" for a in auto])
        if "?" in line_sex:                   # hangmagasság nélküli csoport: a hozzá leghasonlóbb nemű csoporthoz
            ks, C, _cp = _centroids(X, pitch, dur, auto)
            known = [c for c in ks if sex.get(c)]
            for c in ks:
                if not sex.get(c) and known:
                    best = max(known, key=lambda b: float(C[ks.index(c)] @ C[ks.index(b)]))
                    line_sex[auto == c] = sex[best]
        if (line_sex == "f").any() and (line_sex == "m").any():
            labels = np.full(n, -1)
            nxt = 0
            for g, kk in (("f", nf), ("m", nm)):
                idx = np.where(line_sex == g)[0]
                if not len(idx):
                    continue
                sub = _group(X[idx], [pitch[i] for i in idx], dur[idx], min(kk, len(idx)))
                labels[idx] = nxt + sub
                nxt += int(sub.max()) + 1
            labels[labels < 0] = 0
    if labels is None:
        labels = _group(X, pitch, dur, k)

    # a csoportok sorszáma: a megadott nemek sorrendjében (pl. B1-B2 nő, B3-B5 férfi), azon belül a
    # többet beszélő előre; ismeretlen nemű helyek a végén
    talk = {c: dur[labels == c].sum() for c in set(labels.tolist())}
    csex = _sexes(X, pitch, dur, labels)
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
    remap = {v: i for i, v in enumerate(sorted(order.values()))}     # sorszám-hézagok nélkül
    ks, C, _cp = _centroids(X, pitch, dur, labels)
    for i, (ln, l) in enumerate(zip(lines, labels)):
        ln["speaker"] = int(remap[order[int(l)]])
        ln["spk_sim"] = round(float(X[i] @ C[ks.index(int(l))]), 3)
    info = []
    for c in sorted(order, key=lambda c: order[c]):
        ps = [p for p, l in zip(pitch, labels) if l == c and p]
        g = {"f": "nő", "m": "férfi"}.get(csex[c], "?")
        info.append(f"{remap[order[c]] + 1}. beszélő ({g}) {talk[c]:.0f} mp" + (f", ~{np.median(ps):.0f} Hz" if ps else ""))
    log(f"Beszélők: {len(order)} (" + "; ".join(info) + ")")
    return lines
