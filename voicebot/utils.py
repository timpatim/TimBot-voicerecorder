"""Petits utilitaires partagés : formats de durée et niveau sonore."""
import numpy as np


def fmt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def dbfs(pcm: bytes) -> float:
    """Niveau RMS d'un bloc PCM 16 bits, en dB par rapport au maximum (0 = pleine échelle)."""
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if a.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(a * a)))
    return 20 * float(np.log10(max(rms, 1e-9) / 32768.0))
