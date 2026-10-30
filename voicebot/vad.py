"""Détection de voix (VAD) : repère les trames de parole et ignore les silences."""
from collections import deque

import numpy as np

from .config import SET
from .constants import RATE


PRE_ROLL_FRAMES = max(0, SET["vad.pre_roll_ms"] // 20)
START_RING = SET["vad.debut_fenetre"]
START_VOICED = min(SET["vad.debut_trames_voisees"], START_RING)
HANGOVER = int(RATE * SET["vad.silence_fin_ms"] / 1000)
TRAIL = int(RATE * SET["vad.marge_fin_ms"] / 1000)
MAX_UTTERANCE = RATE * SET["vad.enonce_max_s"]
MIN_VOICED_FRAMES = SET["vad.min_trames_voisees"]
MIN_DBFS = SET["vad.niveau_min_dbfs"]


class _WebRtcVad:
    def __init__(self, aggressiveness: int):
        import webrtcvad
        self.v = webrtcvad.Vad(aggressiveness)

    def __call__(self, frame: bytes) -> bool:
        return self.v.is_speech(frame, RATE)


class _EnergyVad:
    """Repli rudimentaire si webrtcvad n'est pas installé."""

    def __init__(self, threshold: float):
        self.threshold = threshold

    def __call__(self, frame: bytes) -> bool:
        a = np.frombuffer(frame, dtype="<i2").astype(np.float32)
        return float(np.sqrt(np.mean(a * a))) > self.threshold


def make_vad_factory():
    """Un détecteur de voix par utilisateur (webrtcvad garde un état interne)."""
    try:
        import webrtcvad  # noqa: F401
        agg = SET["vad.agressivite"]
        return lambda: _WebRtcVad(agg)
    except Exception as e:
        print(f"[live] webrtcvad indisponible ({e!r}) : VAD par énergie en repli "
              "(pip install webrtcvad-wheels pour un vrai VAD)")
        thr = SET["vad.seuil_energie_rms"]
        return lambda: _EnergyVad(thr)


class VadState:
    """État de détection de voix d'un utilisateur."""

    def __init__(self, vad):
        self.vad = vad
        self.rem = b""                 # reste de trame < 20 ms
        self.next = None               # fin (en échantillons) du dernier chunk reçu
        self.in_speech = False
        self.ring = deque(maxlen=START_RING)
        self.pre = deque(maxlen=PRE_ROLL_FRAMES)   # (début, trame) juste avant la parole
        self.buf = bytearray()
        self.start = 0
        self.last_voice_end = 0
        self.voiced = 0
