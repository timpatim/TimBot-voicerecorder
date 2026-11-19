"""Rejeu d'une session enregistrée avec d'autres réglages, pour mesurer ce qui améliore (ou dégrade) le transcript.

Principe : les WAV par joueur de la session sont redécoupés en énoncés exactement comme le fait la transcription en direct
(même logique que `live.py`), mais avec des paramètres variables, puis passés à WhisperX. Chaque « variante » est un jeu de
réglages ; on compare leurs statistiques (fragmentation, hallucinations, débit impossible, lignes tronquées) et, si on a
un extrait corrigé à la main, leur taux d'erreurs (WER).

Module indépendant de Discord. N'écrit rien dans la session : tout va dans un dossier de comparaison.
"""
import hashlib
import json
import re
import statistics
import time
import wave
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from .config import SET
from .constants import FRAME, FRAME_BYTES, RATE
from .utils import dbfs, fmt_ts

PHRASES_COURTES = "au revoir, bye, amen, merci, à bientôt, a bientôt"
DEBIT_MAX = 7.0                        # mots par seconde au-delà desquels la ligne est jugée impossible


@dataclass(frozen=True)
class Params:
    """Un jeu de réglages. Les valeurs par défaut sont celles de ton config.ini (variante « actuel »)."""
    nom: str = "actuel"
    agressivite: int = 2
    pre_roll_ms: int = 300
    debut_fenetre: int = 5
    debut_trames_voisees: int = 3
    silence_fin_ms: int = 700
    marge_fin_ms: int = 200
    enonce_max_s: int = 20
    min_trames_voisees: int = 22
    niveau_min_dbfs: float = -45.0
    mots_seuls_ignores: str = "merci, au revoir, bye, amen"
    # --- idées à tester (inactives par défaut) ---
    fusion_s: float = 0.0              # fusionne deux énoncés du même joueur séparés de moins de N secondes
    min_s: float = 0.0                 # écarte les énoncés plus courts que N secondes
    filtre_duree_s: float = 0.0        # rejette les phrases de PHRASES_COURTES si l'énoncé dure moins de N secondes
    phrases_courtes: str = PHRASES_COURTES
    contexte: bool = False             # passe la fin de la phrase précédente du joueur comme contexte à Whisper
    contexte_max_s: float = 15.0       # ... si elle date de moins de N secondes
    contexte_car: int = 200            # ... et au plus N caractères
    # --- traitement de l'audio avant le découpage (inactif par défaut) ---
    gain_cible_dbfs: float = 0.0       # gain fixe par joueur : amène le niveau de sa parole à N dBFS (ex. -20) ; 0 = aucun
    passe_haut_hz: float = 0.0         # filtre passe-haut (ex. 80 Hz : retire les grondements) ; 0 = aucun
    debruitage: str = ""               # « noisereduce » ou « deepfilter » (bibliothèques à installer) ; vide = aucun
    # --- transcription ---
    alignement: bool = True            # False : sans l'alignement mot à mot de WhisperX (timestamps de segment bruts)
    redecoupe_s: float = 0.0           # énoncé d'au moins N s dont le texte est long et sans ponctuation finale : coupé à sa
                                       # plus longue pause et retranscrit en deux morceaux (gardé seulement si ça améliore) ; 0 = non


def from_config(nom: str = "actuel") -> Params:
    """Les réglages en vigueur dans config.ini."""
    return Params(nom=nom, agressivite=SET["vad.agressivite"], pre_roll_ms=SET["vad.pre_roll_ms"],
                  debut_fenetre=SET["vad.debut_fenetre"], debut_trames_voisees=SET["vad.debut_trames_voisees"],
                  silence_fin_ms=SET["vad.silence_fin_ms"], marge_fin_ms=SET["vad.marge_fin_ms"],
                  enonce_max_s=SET["vad.enonce_max_s"], min_trames_voisees=SET["vad.min_trames_voisees"],
                  niveau_min_dbfs=SET["vad.niveau_min_dbfs"], mots_seuls_ignores=SET["whisper.mots_seuls_ignores"])


def presets(base: Params) -> dict:
    """Variantes proposées. reglages -> contexte s'ajoutent les unes aux autres ; les suivantes partent toutes de « actuel »."""
    reglages = replace(base, nom="reglages", silence_fin_ms=1100, min_trames_voisees=22,
                       mots_seuls_ignores=base.mots_seuls_ignores + ", au revoir, bye, amen")
    filtre = replace(base, nom="filtre", silence_fin_ms=1100, min_trames_voisees=22, filtre_duree_s=0.6,
                     mots_seuls_ignores="merci")
    fusion = replace(filtre, nom="fusion", fusion_s=1.2)
    contexte = replace(fusion, nom="contexte", contexte=True)
    gain = replace(base, nom="gain", gain_cible_dbfs=-20.0)
    passehaut = replace(base, nom="passehaut", passe_haut_hz=80.0)
    bruit = replace(base, nom="bruit", debruitage="noisereduce")
    sansalign = replace(base, nom="sansalign", alignement=False)
    redecoupe = replace(base, nom="redecoupe", redecoupe_s=10.0)
    combine = replace(base, nom="combine", gain_cible_dbfs=-20.0, passe_haut_hz=80.0, redecoupe_s=10.0)
    return {p.nom: p for p in (base, reglages, filtre, fusion, contexte, gain, passehaut, bruit, sansalign, redecoupe, combine)}


def parse_variant(spec: str, base: Params) -> Params:
    """`nom=cle:valeur,cle:valeur` -> variante personnalisée dérivée de `base`. Ex. `test=silence_fin_ms:900,fusion_s:1`."""
    nom, _, rest = spec.partition("=")
    fields = Params.__dataclass_fields__
    values = {}
    for item in filter(None, (x.strip() for x in rest.split(","))):
        key, _, raw = item.partition(":")
        key = key.strip()
        if key not in fields or key == "nom":
            raise ValueError(f"réglage inconnu « {key} » (valides : {', '.join(k for k in fields if k != 'nom')})")
        kind = type(getattr(base, key))
        raw = raw.strip()
        values[key] = (raw.lower() in ("1", "oui", "true", "vrai", "on")) if kind is bool else kind(raw)
    return replace(base, nom=nom.strip(), **values)


# --- audio ----------------------------------------------------------------------------------------

def read_wav(path: Path) -> np.ndarray:
    """WAV 16 kHz mono 16 bits -> tableau int16."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path.name} : format inattendu ({w.getframerate()} Hz, {w.getnchannels()} voie(s))")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def session_tracks(session_dir: Path) -> list:
    """-> [(identifiant, nom du joueur, chemin du WAV)] d'après meta.json (sinon, les WAV du dossier audio)."""
    meta = session_dir / "meta.json"
    out = []
    if meta.exists():
        for p in json.loads(meta.read_text(encoding="utf-8")).get("participants", []):
            f = session_dir / "audio" / (p.get("file") or "")
            if p.get("file") and f.exists():
                out.append((p["id"], p.get("name") or p["id"], f))
    if not out:
        out = [(f.stem, f.stem, f) for f in sorted((session_dir / "audio").glob("*.wav"))]
    return out


def voiced_flags(pcm: np.ndarray, aggressiveness: int) -> list:
    """Une valeur par trame de 20 ms : voix détectée ou non (webrtcvad, sinon VAD par énergie en repli)."""
    n = len(pcm) // FRAME
    raw = pcm[: n * FRAME].tobytes()
    try:
        import webrtcvad
        v = webrtcvad.Vad(aggressiveness)
        return [v.is_speech(raw[i * FRAME_BYTES:(i + 1) * FRAME_BYTES], RATE) for i in range(n)]
    except Exception:
        from .vad import _EnergyVad
        vad = _EnergyVad(SET["vad.seuil_energie_rms"])
        return [vad(raw[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]) for i in range(n)]


@dataclass
class Utterance:
    uid: str
    nom: str
    start: int                 # échantillons, timeline de la session
    end: int
    voiced: int = 0
    level: float = 0.0

    @property
    def duree(self) -> float:
        return (self.end - self.start) / RATE


def segment(flags: list, params: Params, n_samples: int | None = None) -> tuple:
    """Découpe en énoncés : la même logique que `LiveTranscriber._frame/_end`, avec `params`.
    -> (énoncés [(début, fin, trames voisées)], {raison de rejet: nombre})."""
    pre_frames = max(0, params.pre_roll_ms // 20)
    start_voiced = min(params.debut_trames_voisees, params.debut_fenetre)
    hangover = int(RATE * params.silence_fin_ms / 1000)
    trail = int(RATE * params.marge_fin_ms / 1000)
    max_len = RATE * params.enonce_max_s
    pre, ring = deque(maxlen=pre_frames), deque(maxlen=params.debut_fenetre)
    out, rejected = [], {"trop court": 0, "trop peu de voix": 0}
    state = {"in": False, "start": 0, "voiced": 0, "last": 0}

    def end_utt(end_sample: int):
        n = max(0, end_sample - state["start"])
        if n_samples is not None:
            n = min(n, n_samples - state["start"])
        v, s = state["voiced"], state["start"]
        state["in"], state["voiced"] = False, 0
        pre.clear()
        ring.clear()
        if n < FRAME * 5:
            rejected["trop court"] += 1
        elif v < params.min_trames_voisees:
            rejected["trop peu de voix"] += 1
        else:
            out.append((s, s + n, v))

    for i, voiced in enumerate(flags):
        start = i * FRAME
        end = start + FRAME
        if voiced:
            state["last"] = end
        if not state["in"]:
            pre.append(start)
            ring.append(voiced)
            if sum(ring) >= start_voiced:
                state["in"], state["start"], state["voiced"] = True, pre[0], sum(ring)
            continue
        if voiced:
            state["voiced"] += 1
        if end - state["last"] >= hangover:
            end_utt(state["last"] + trail)
        elif end - state["start"] >= max_len:
            end_utt(end)
    if state["in"]:
        end_utt(state["last"] + trail)
    return out, rejected


def merge_close(utts: list, params: Params) -> list:
    """Fusionne les énoncés d'un même joueur séparés de moins de `fusion_s` (audio réel entre les deux conservé)."""
    if params.fusion_s <= 0 or not utts:
        return utts
    gap, cap = int(params.fusion_s * RATE), params.enonce_max_s * RATE
    merged = [replace(utts[0])]
    for u in utts[1:]:
        last = merged[-1]
        if u.start - last.end <= gap and u.end - last.start <= cap:
            last.end, last.voiced = u.end, last.voiced + u.voiced
        else:
            merged.append(replace(u))
    return merged


def build_utterances(pcm: np.ndarray, uid: str, nom: str, params: Params, flags: list | None = None) -> tuple:
    """Énoncés d'un joueur pour `params`. -> (énoncés, rejets avant Whisper {raison: n})."""
    flags = flags if flags is not None else voiced_flags(pcm, params.agressivite)
    raw, rejected = segment(flags, params, len(pcm))
    rejected = dict(rejected, **{"trop faible": 0, "trop bref (min_s)": 0})
    utts = []
    for s, e, v in raw:
        level = dbfs(pcm[s:e].tobytes())
        if level < params.niveau_min_dbfs:
            rejected["trop faible"] += 1
            continue
        utts.append(Utterance(uid, nom, s, e, v, level))
    utts = merge_close(utts, params)
    if params.min_s > 0:
        kept = [u for u in utts if u.duree >= params.min_s]
        rejected["trop bref (min_s)"] = len(utts) - len(kept)
        utts = kept
    return utts, rejected


# --- rejet de textes ------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower().replace("’", "'")))


def rejection(text: str, utt_duration: float, params: Params) -> str | None:
    """Pourquoi écarter ce texte (None = le garder). Les règles de `live.py` + le filtre selon la durée."""
    if not re.search(r"\w", text) or re.search(SET["whisper.filtre_hallucinations"], text, re.I):
        return "hallucination probable"
    words = re.findall(r"\w+", text.lower())
    solo = {w for w in re.split(r"[,\s;]+", params.mots_seuls_ignores.lower()) if w}
    if solo and words and all(w in solo for w in words):
        return "mot isolé ignoré"
    if params.filtre_duree_s > 0 and utt_duration < params.filtre_duree_s:
        phrases = {_norm(p) for p in params.phrases_courtes.split(",") if p.strip()}
        if _norm(text) in phrases:
            return "phrase typique sur un énoncé très court"
    return None


# --- traitement de l'audio ------------------------------------------------------------------------

GAIN_MIN_DB, GAIN_MAX_DB = -12.0, 24.0          # le gain par joueur reste dans cette plage
BLOC_S = 60                                     # le filtrage et le débruitage travaillent par blocs de 60 s (mémoire)
_DF = None                                      # modèle DeepFilterNet, chargé une seule fois


def _to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x), -32768, 32767).astype("<i2")


def frame_power(pcm: np.ndarray, n: int) -> np.ndarray:
    """Puissance moyenne (carré de l'amplitude) de chacune des n premières trames de 20 ms."""
    out = np.empty(n)
    step = 20000
    for i in range(0, n, step):
        j = min(n, i + step)
        block = pcm[i * FRAME:j * FRAME].astype(np.float32).reshape(j - i, FRAME)
        out[i:j] = np.mean(block * block, axis=1)
    return out


def speech_level_dbfs(pcm: np.ndarray, flags: list) -> float | None:
    """Niveau RMS (dBFS) des trames où il y a de la voix ; None s'il n'y en a aucune."""
    n = min(len(flags), len(pcm) // FRAME)
    voiced = np.asarray(flags[:n], dtype=bool)
    if n == 0 or not voiced.any():
        return None
    power = float(frame_power(pcm, n)[voiced].mean())
    return None if power <= 0 else 10 * float(np.log10(power / 32768.0 ** 2))


def apply_gain(pcm: np.ndarray, gain_db: float) -> tuple:
    """-> (audio amplifié, nombre d'échantillons écrêtés)."""
    factor, out, clipped = 10 ** (gain_db / 20), np.empty(len(pcm), dtype="<i2"), 0
    step = RATE * BLOC_S
    for i in range(0, len(pcm), step):
        y = pcm[i:i + step].astype(np.float32) * factor
        clipped += int(np.count_nonzero(np.abs(y) > 32767))
        out[i:i + step] = _to_int16(y)
    return out, clipped


def high_pass(pcm: np.ndarray, hz: float) -> np.ndarray:
    """Filtre passe-haut de Butterworth d'ordre 2 (retire les grondements sans toucher à la voix)."""
    try:
        from scipy.signal import butter, sosfilt
    except ImportError as e:
        raise RuntimeError("le filtre passe-haut demande scipy (`pip install scipy`)") from e
    sos = butter(2, hz, btype="highpass", fs=RATE, output="sos")
    out, zi, step = np.empty(len(pcm), dtype="<i2"), np.zeros((sos.shape[0], 2)), RATE * BLOC_S
    for i in range(0, len(pcm), step):
        y, zi = sosfilt(sos, pcm[i:i + step].astype(np.float64), zi=zi)
        out[i:i + step] = _to_int16(y)
    return out


def denoise(pcm: np.ndarray, method: str, region: tuple | None = None) -> np.ndarray:
    """Réduction de bruit. `region` (début, fin) en échantillons : seule cette zone (plus 5 s de marge) est traitée."""
    if method not in ("noisereduce", "deepfilter"):
        raise ValueError(f"débruitage inconnu « {method} » (valeurs : noisereduce, deepfilter)")
    lo, hi = 0, len(pcm)
    if region:
        lo = max(0, region[0] - 5 * RATE)
        hi = len(pcm) if region[1] is None else min(len(pcm), region[1] + 5 * RATE)
    out, step = pcm.copy(), RATE * BLOC_S
    if method == "noisereduce":
        try:
            import noisereduce as nr
        except ImportError as e:
            raise RuntimeError("le débruitage « noisereduce » demande `pip install noisereduce`") from e
        for i in range(lo, hi, step):
            x = pcm[i:min(hi, i + step)].astype(np.float32) / 32768.0
            out[i:i + len(x)] = _to_int16(nr.reduce_noise(y=x, sr=RATE, stationary=True, prop_decrease=0.8) * 32768.0)
        return out
    global _DF
    try:
        import torch
        from df.enhance import enhance, init_df
        from scipy.signal import resample_poly
    except ImportError as e:
        raise RuntimeError("le débruitage « deepfilter » demande `pip install deepfilternet` (et scipy)") from e
    if _DF is None:
        _DF = init_df()
    model, df_state, _ = _DF
    sr = df_state.sr()
    for i in range(lo, hi, step):
        x = pcm[i:min(hi, i + step)].astype(np.float32) / 32768.0
        y = enhance(model, df_state, torch.from_numpy(resample_poly(x, sr, RATE).astype(np.float32)).unsqueeze(0))
        y = resample_poly(y.squeeze(0).numpy(), RATE, sr)[:len(x)] * 32768.0
        out[i:i + len(y)] = _to_int16(y)
    return out


def prep_active(params: Params) -> bool:
    return bool(params.gain_cible_dbfs or params.passe_haut_hz or params.debruitage)


def prepare_audio(pcm: np.ndarray, params: Params, region: tuple | None = None) -> tuple:
    """Passe-haut, débruitage puis gain par joueur (dans cet ordre : le niveau est mesuré sur l'audio déjà nettoyé).
    -> (audio traité, informations : niveau de parole, gain appliqué, échantillons écrêtés)."""
    info, out = {}, pcm
    if params.passe_haut_hz > 0:
        out = high_pass(out, params.passe_haut_hz)
    if params.debruitage:
        out = denoise(out, params.debruitage, region)
    if params.gain_cible_dbfs:
        level = speech_level_dbfs(out, voiced_flags(out, params.agressivite))
        if level is None:
            info["note"] = "aucune parole détectée : pas de gain"
        else:
            gain = min(max(params.gain_cible_dbfs - level, GAIN_MIN_DB), GAIN_MAX_DB)
            out, clipped = apply_gain(out, gain)
            info.update(niveau_parole_dbfs=round(level, 1), gain_db=round(gain, 1),
                        ecretes_pct=round(100 * clipped / max(1, len(out)), 3))
    return out, info


@contextmanager
def alignment(engine, enabled: bool):
    """Désactive temporairement l'alignement mot à mot de WhisperX (`engine.align_model`)."""
    if enabled or engine is None or getattr(engine, "align_model", None) is None:
        yield
        return
    saved, engine.align_model = engine.align_model, None
    try:
        yield
    finally:
        engine.align_model = saved


SUSPECT_MOTS = 12                       # une ligne d'au moins 12 mots sans ponctuation finale est suspecte (texte souvent faux)
RE_FIN_PHRASE = re.compile(r"[.!?…\"»)]$")


def suspect_count(segs: list) -> int:
    return sum(1 for s in segs if len(re.findall(r"[\w'’]+", s.get("text", ""))) >= SUSPECT_MOTS
               and not RE_FIN_PHRASE.search(s.get("text", "").strip()))


def longest_pause(flags: list, start: int, end: int) -> int | None:
    """Échantillon (milieu) de la plus longue pause d'au moins 100 ms dans le tiers central d'un énoncé, sinon None."""
    f0, f1 = start // FRAME, min(end // FRAME, len(flags))
    if f1 - f0 < 10:
        return None
    lo, hi = f0 + int(0.2 * (f1 - f0)), f0 + int(0.8 * (f1 - f0))
    best, best_len, run_start = None, 0, None
    for i in range(lo, hi + 1):
        quiet = i < hi and not flags[i]
        if quiet and run_start is None:
            run_start = i
        elif not quiet and run_start is not None:
            if i - run_start > best_len:
                best_len, best = i - run_start, run_start + (i - run_start) // 2
            run_start = None
    return best * FRAME if best is not None and best_len >= 5 else None


# --- transcription --------------------------------------------------------------------------------

def set_prompt(engine, prompt: str | None) -> bool:
    """Fixe le texte de contexte de Whisper pour les prochains appels. Retourne False si la version installée ne le permet pas
    (la variante « contexte » est alors sans effet, et le rapport le dit)."""
    setter = getattr(engine, "set_prompt", None)
    if setter:
        setter(prompt)
        return True
    opts = getattr(getattr(engine, "model", None), "options", None)
    if opts is None:
        return False
    try:
        if hasattr(opts, "_replace"):
            engine.model.options = opts._replace(initial_prompt=prompt)
        else:
            import dataclasses
            engine.model.options = dataclasses.replace(opts, initial_prompt=prompt)
        return True
    except Exception:
        return False


class AsrCache:
    """Mémorise les résultats de Whisper (même audio, même contexte = même résultat) : les variantes qui partagent des énoncés
    ne les retranscrivent pas, et une comparaison interrompue peut reprendre."""

    def __init__(self, path: Path | None):
        self.path, self.data = path, {}
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    k, segs = json.loads(line)
                    self.data[k] = segs
                except ValueError:
                    continue

    @staticmethod
    def key(audio: np.ndarray, prompt: str | None, tag: str = "") -> str:
        """`tag` distingue les réglages de Whisper qui changent le résultat sans changer l'audio (ex. sans alignement)."""
        return hashlib.sha1(audio.tobytes() + (prompt or "").encode("utf-8") + tag.encode("utf-8")).hexdigest()

    def get(self, k):
        return self.data.get(k)

    def put(self, k, segs):
        self.data[k] = segs
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps([k, segs], ensure_ascii=False) + "\n")


def transcribe_variant(params: Params, tracks: list, engine, window: tuple | None = None, cache: AsrCache | None = None,
                       progress=None, flags_cache: dict | None = None, pcm_cache: dict | None = None) -> dict:
    """Rejoue `tracks` [(id, nom, wav)] avec `params`.
    -> {'rows', 'rejetes', 'avant_whisper', 'enonces', 'redecoupes', 'prep', 'stats_asr'}.

    `window` : (début_s, fin_s) pour ne traiter qu'un extrait. `engine=None` : découpage seul (pas de Whisper)."""
    say = progress or (lambda _t: None)
    rows, rejected_rows, before, cuts, prep_info = [], [], {}, [], {}
    acc = {"asr_s": 0.0, "audio_s": 0.0, "hits": 0, "ctx_ok": True}
    if engine is not None:
        set_prompt(engine, None)                        # repart sans contexte hérité d'une variante précédente
    lo, hi = (int(window[0] * RATE), int(window[1] * RATE)) if window else (0, None)
    tag = "" if params.alignement else "sans-alignement"

    def decode(pcm, u, prompt) -> list:
        audio = pcm[u.start:u.end].astype(np.float32) / 32768.0
        ck = AsrCache.key(audio, prompt, tag)
        segs = cache.get(ck) if cache else None
        if segs is None:
            if params.contexte:
                acc["ctx_ok"] = set_prompt(engine, prompt) and acc["ctx_ok"]
            t = time.monotonic()
            with alignment(engine, params.alignement):
                segs = [{"start": float(s.get("start", 0.0)), "end": float(s.get("end", u.duree)), "text": s.get("text", "")}
                        for s in (engine.transcribe(audio) or [])]
            acc["asr_s"] += time.monotonic() - t
            if cache:
                cache.put(ck, segs)
        else:
            acc["hits"] += 1
        acc["audio_s"] += u.duree
        return segs

    def run(pcm, flags, u, prompt, depth=0) -> list:
        """-> [(énoncé, segments)] : l'énoncé tel quel, ou ses morceaux s'il a été redécoupé avec profit."""
        segs = decode(pcm, u, prompt)
        if params.redecoupe_s > 0 and depth < 2 and u.duree >= params.redecoupe_s and suspect_count(segs):
            mid = longest_pause(flags, u.start, u.end)
            if mid is not None and mid - u.start >= RATE and u.end - mid >= RATE:
                parts = run(pcm, flags, replace(u, end=mid), prompt, depth + 1) + \
                    run(pcm, flags, replace(u, start=mid), prompt, depth + 1)
                if sum(suspect_count(sg) for _p, sg in parts) < suspect_count(segs):
                    cuts.append({"name": u.nom, "start": round(u.start / RATE, 1), "duree": round(u.duree, 1),
                                 "avant": " ".join(x["text"].strip() for x in segs),
                                 "apres": " | ".join(" ".join(x["text"].strip() for x in sg) for _p, sg in parts)})
                    return parts
        return [(u, segs)]

    utt_count = 0
    for uid, nom, wav in tracks:
        raw = (pcm_cache or {}).get(wav) if pcm_cache is not None else None
        if raw is None:
            raw = read_wav(wav)
            if pcm_cache is not None:
                pcm_cache.clear()                       # un seul WAV en mémoire à la fois
                pcm_cache[wav] = raw
        if prep_active(params):
            say(f"{nom} : traitement de l'audio")
            pcm, prep_info[nom] = prepare_audio(raw, params, (lo, hi))
            flags = voiced_flags(pcm, params.agressivite)
        else:
            pcm = raw
            fk = (wav, params.agressivite)
            flags = flags_cache.get(fk) if flags_cache is not None else None
            if flags is None:
                flags = voiced_flags(pcm, params.agressivite)
                if flags_cache is not None:
                    flags_cache[fk] = flags
        utts, rej = build_utterances(pcm, uid, nom, params, flags)
        for k, v in rej.items():
            before[k] = before.get(k, 0) + v
        utts = [u for u in utts if u.end > lo and (hi is None or u.start < hi)]
        utt_count += len(utts)
        say(f"{nom} : {len(utts)} énoncé(s)")
        if engine is None:
            continue
        last_text, last_end = "", -1e9
        for i, u in enumerate(utts, 1):
            prompt = None
            if params.contexte and last_text and u.start / RATE - last_end <= params.contexte_max_s:
                prompt = last_text[-params.contexte_car:]
            for piece, segs in run(pcm, flags, u, prompt):
                for s in segs:
                    text = (s["text"] or "").strip()
                    if not text:
                        continue
                    s0 = min(max(s["start"], 0.0), piece.duree)
                    s1 = min(max(s["end"], s0), piece.duree)
                    row = {"name": nom, "start": round(piece.start / RATE + s0, 3), "end": round(piece.start / RATE + s1, 3),
                           "text": text, "utt_s": round(piece.duree, 2)}
                    reason = rejection(text, piece.duree, params)
                    if reason:
                        rejected_rows.append({**row, "reason": reason})
                    else:
                        rows.append(row)
                        last_text, last_end = text, piece.end / RATE
            if i % 50 == 0:
                say(f"{nom} : {i}/{len(utts)}")
    rows.sort(key=lambda r: (r["start"], r["end"]))
    return {"rows": rows, "rejetes": rejected_rows, "avant_whisper": before, "enonces": utt_count, "redecoupes": cuts,
            "prep": prep_info,
            "stats_asr": {"asr_s": acc["asr_s"], "audio_s": acc["audio_s"], "cache": acc["hits"],
                          "contexte_applique": (acc["ctx_ok"] if params.contexte else None)}}


# --- mesures --------------------------------------------------------------------------------------

def _words(text: str) -> list:
    return re.findall(r"[\w'’]+", text.lower())


def metrics(rows: list, rejected: list | None = None) -> dict:
    """Indicateurs de qualité sans référence (le moins est le mieux, sauf « lignes » et « durée médiane »)."""
    n = len(rows)
    if not n:
        return {"lignes": 0}
    dur = [max(0.0, r["end"] - r["start"]) for r in rows]
    fast = [r for r in rows if r["end"] > r["start"] and len(_words(r["text"])) >= 3
            and len(_words(r["text"])) / (r["end"] - r["start"]) > DEBIT_MAX]
    typical = {"au revoir", "bye", "amen", "merci", "a bientot", "à bientôt"}
    hall = [r for r in rows if _norm(r["text"]) in typical]
    cut = [r for r in rows if len(_words(r["text"])) >= 4 and not re.search(r"[.!?…\"»)]$", r["text"].strip())]
    return {"lignes": n,
            "durée médiane (s)": round(statistics.median(dur), 2),
            "% < 1 s": round(100 * sum(d < 1 for d in dur) / n, 1),
            "% < 0,5 s": round(100 * sum(d < 0.5 for d in dur) / n, 1),
            "% ≤ 2 mots": round(100 * sum(len(_words(r["text"])) <= 2 for r in rows) / n, 1),
            f"% débit > {DEBIT_MAX:.0f} mots/s": round(100 * len(fast) / n, 1),
            "« au revoir / bye / amen / merci »": len(hall),
            "% sans ponctuation finale (≥ 4 mots)": round(100 * len(cut) / n, 1),
            f"lignes de {SUSPECT_MOTS} mots ou plus sans ponctuation finale": sum(
                1 for r in cut if len(_words(r["text"])) >= SUSPECT_MOTS),
            "mots au total": sum(len(_words(r["text"])) for r in rows),
            "rejetées par le filtre": len(rejected or [])}


def wer(reference: list, hypothesis: list, window: tuple | None = None) -> dict:
    """Taux d'erreurs sur les mots, joueur par joueur, entre deux listes de lignes {name, start, text}.
    -> {'wer': %, 'erreurs': n, 'mots_ref': n}. `window` : (début_s, fin_s)."""
    def pick(rows, name):
        return [r for r in rows if r["name"] == name and (window is None or window[0] <= r["start"] < window[1])]
    errors = total = 0
    for name in {r["name"] for r in reference}:
        ref = _words(" ".join(r["text"] for r in sorted(pick(reference, name), key=lambda r: r["start"])))
        hyp = _words(" ".join(r["text"] for r in sorted(pick(hypothesis, name), key=lambda r: r["start"])))
        errors += _edit_distance(ref, hyp)
        total += len(ref)
    return {"wer": round(100 * errors / total, 1) if total else None, "erreurs": errors, "mots_ref": total}


def _edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


RE_LIGNE = re.compile(r"^\[(\d+):(\d\d):(\d\d)\.(\d{3}) → (\d+):(\d\d):(\d\d)\.(\d{3})\] (.+?) : (.*)$")


def read_transcript(path: Path) -> list:
    """Lit un transcript au format du bot (transcript_live.txt). -> [{'name','start','end','text'}]."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = RE_LIGNE.match(line)
        if m:
            g = m.groups()
            sec = lambda h, mi, s, ms: int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000
            rows.append({"name": g[8], "start": sec(*g[0:4]), "end": sec(*g[4:8]), "text": g[9]})
    return rows


def transcript_text(rows: list, title: str) -> str:
    head = [f"# {title}", "# Timestamps relatifs au début de la session.", ""]
    return "\n".join(head + [f"[{fmt_ts(r['start'])} → {fmt_ts(r['end'])}] {r['name']} : {r['text']}" for r in rows]) + "\n"


def differences(a: list, b: list, window_s: int = 30, limit: int = 120) -> list:
    """Fenêtres de `window_s` secondes où les textes de A et de B diffèrent, les plus différentes d'abord.
    -> [(début_s, texte A, texte B, nombre d'écarts)]."""
    def by_window(rows):
        out = {}
        for r in rows:
            out.setdefault(int(r["start"] // window_s), []).append(f"{r['name']}: {r['text']}")
        return out
    wa, wb = by_window(a), by_window(b)
    found = []
    for k in set(wa) | set(wb):
        ta, tb = "\n".join(wa.get(k, [])), "\n".join(wb.get(k, []))
        if _norm(ta) != _norm(tb):
            found.append((k * window_s, ta, tb, _edit_distance(_words(ta), _words(tb))))
    return sorted(found, key=lambda x: -x[3])[:limit]


def report(results: dict, wers: dict | None = None) -> str:
    """Rapport Markdown : tableau des indicateurs par variante."""
    names = list(results)
    table = {n: metrics(r["rows"], r["rejetes"]) for n, r in results.items()}
    keys = [k for k in table[names[0]] if k != "mots au total"] + ["mots au total"]
    lines = ["# Comparaison de réglages de transcription", "",
             "| Indicateur | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(table[n].get(k, "")) for n in names) + " |")
    lines.append("| énoncés envoyés à Whisper | " + " | ".join(str(results[n]["enonces"]) for n in names) + " |")
    if wers:
        lines.append("| **WER** (% d'erreurs, ref. corrigée) | " + " | ".join(
            str(wers[n]["wer"]) + " %" if wers.get(n) and wers[n]["wer"] is not None else "—" for n in names) + " |")
    lines.append("| énoncés redécoupés (plus longue pause) | " + " | ".join(str(len(results[n].get("redecoupes", [])))
                                                                            for n in names) + " |")
    lines += ["", "## Écartés avant Whisper (découpage)", "", "| Raison | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    reasons = sorted({k for r in results.values() for k in r["avant_whisper"]})
    for k in reasons:
        lines.append(f"| {k} | " + " | ".join(str(results[n]["avant_whisper"].get(k, 0)) for n in names) + " |")
    lines += ["", "## Temps de calcul", "", "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    lines.append("| Whisper (s) | " + " | ".join(f"{results[n]['stats_asr']['asr_s']:.0f}" for n in names) + " |")
    lines.append("| résultats réutilisés du cache | " + " | ".join(str(results[n]["stats_asr"]["cache"]) for n in names) + " |")
    players = sorted({p for r in results.values() for p in r.get("prep", {})})
    if players:
        lines += ["", "## Traitement de l'audio par joueur (gain appliqué en dB · niveau de parole avant gain en dBFS)", "",
                  "| Joueur | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for p in players:
            cells = []
            for n in names:
                i = results[n].get("prep", {}).get(p)
                cells.append("—" if not i else (f"{i['gain_db']:+.1f} dB · {i['niveau_parole_dbfs']} dBFS"
                                                + (f" · {i['ecretes_pct']} % écrêtés" if i.get("ecretes_pct") else "")
                                                if "gain_db" in i else i.get("note", "filtré")))
            lines.append(f"| {p} | " + " | ".join(cells) + " |")
    ctx = [n for n in names if results[n]["stats_asr"]["contexte_applique"] is False]
    if ctx:
        lines += ["", f"⚠️ Variante(s) {', '.join(ctx)} : la version de WhisperX installée n'accepte pas de contexte "
                      "par appel, la variante est donc identique à sa précédente."]
    lines += ["", "Lecture : moins il y a de lignes très courtes, de débit impossible, d'« au revoir » et de lignes sans "
                 "ponctuation, mieux c'est. Le nombre de mots au total ne doit pas s'effondrer (sinon on perd de la parole)."]
    return "\n".join(lines) + "\n"
