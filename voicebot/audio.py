"""Archivage audio : un fichier WAV par utilisateur, sur la timeline de la session (chemin prioritaire)."""
import json
import logging
import re
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from discord.ext import voice_recv

from .config import SET
from .constants import RATE


GAP_TOLERANCE = RATE * SET["audio.tolerance_trou_ms"] // 1000


class _DropRtcpNoise(logging.Filter):
    """Masque les 'Received unexpected rtcp packet' de voice_recv, sans toucher aux autres logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "unexpected rtcp packet" not in record.getMessage()


logging.getLogger("discord.ext.voice_recv.reader").addFilter(_DropRtcpNoise())


def to_mono16k(raw: bytes) -> bytes:
    """48 kHz stéréo 16 bits little-endian -> 16 kHz mono 16 bits (moyenne par 3 échantillons)."""
    a = np.frombuffer(raw, dtype="<i2")
    n = (len(a) // 6) * 6
    if n == 0:
        return b""
    m = a[:n].astype(np.int32).reshape(-1, 3, 2).sum(axis=(1, 2)) // 6
    return m.astype("<i2").tobytes()


class Track:
    """Un fichier WAV par utilisateur, sur la timeline de la session.

    L'en-tête WAV est remis à jour à chaque flush (1 s) : le fichier est lisible pendant
    l'enregistrement, et reste valide jusqu'au dernier flush si le bot plante."""

    def __init__(self, path: Path):
        self.f = open(path, "wb", buffering=64 * 1024)
        self.f.write(self._header(0))
        self.written = 0           # en échantillons
        self.finished = False
        self.lock = threading.Lock()

    def write(self, pcm: bytes, expected_start: int):
        """Écrit la trame ; retourne l'échantillon de début, ou None si la piste est fermée."""
        with self.lock:
            if self.finished:
                return None
            gap = expected_start - self.written
            if gap > GAP_TOLERANCE:        # vrai silence : on recale sur l'horloge de la session
                self._pad(gap)
            start = self.written
            self.f.write(pcm)
            self.written += len(pcm) // 2
            return start

    @staticmethod
    def _header(n_bytes: int) -> bytes:
        return (b"RIFF" + struct.pack("<I", 36 + n_bytes) + b"WAVE" + b"fmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
                + b"data" + struct.pack("<I", n_bytes))

    def _sync_header(self):
        n = self.written * 2
        self.f.flush()
        self.f.seek(4)
        self.f.write(struct.pack("<I", 36 + n))
        self.f.seek(40)
        self.f.write(struct.pack("<I", n))
        self.f.seek(0, 2)
        self.f.flush()

    def _pad(self, samples: int):
        remaining = samples * 2
        block = bytes(64_000)
        while remaining > 0:
            n = min(remaining, len(block))
            self.f.write(block[:n])
            remaining -= n
        self.written += samples

    def flush(self):
        with self.lock:
            if not self.finished:
                self._sync_header()

    def finish(self, total: int):
        with self.lock:
            if self.finished:
                return
            if total > self.written:       # tous les fichiers ont la même durée
                self._pad(total - self.written)
            self._sync_header()
            self.f.close()
            self.finished = True


class SessionRecorder(voice_recv.AudioSink):
    def __init__(self, session_id: str, root: Path):
        super().__init__()
        self.session_id = session_id
        self.dir = root / session_id
        (self.dir / "audio").mkdir(parents=True, exist_ok=True)
        self.t0 = datetime.now(timezone.utc)
        self._t0_mono = time.monotonic()
        self.tracks: dict[int, Track] = {}
        self.names: dict[int, str] = {}
        self.files: dict[int, str] = {}
        self.on_chunk = None               # callback(uid, start_sample, pcm_bytes)
        self.ignored_ids: set = set()      # identifiants jamais enregistrés ni transcrits (le bot lui-même)
        self.closed = False
        self.total_samples = None          # durée figée à l'arrêt
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._write_meta(-1)
        threading.Thread(target=self._flusher, daemon=True).start()

    # -- API de l'extension voice_recv --

    def wants_opus(self) -> bool:
        return False                       # on veut du PCM décodé

    def write(self, user, data):
        if self.closed or user is None or user.id in self.ignored_ids:
            return
        pcm = to_mono16k(data.pcm)
        if not pcm:
            return
        uid = user.id
        self.names[uid] = user.display_name
        track = self.tracks.get(uid)
        if track is None:
            with self._lock:
                track = self.tracks.get(uid)
                if track is None:
                    fname = self._filename(uid, user.display_name)
                    self.files[uid] = fname
                    track = self.tracks[uid] = Track(self.dir / "audio" / fname)
        start = track.write(pcm, self.now_samples() - len(pcm) // 2)
        if start is not None and self.on_chunk:
            self.on_chunk(uid, start, pcm)

    def cleanup(self):
        self.stop()

    # -- utilitaires --

    def now_samples(self) -> int:
        return int((time.monotonic() - self._t0_mono) * RATE)

    def _filename(self, uid: int, name: str) -> str:
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or str(uid)
        fname = f"{base}.wav"
        if fname in self.files.values():       # deux utilisateurs au même nom
            fname = f"{base}_{uid}.wav"
        return fname

    def duration_s(self) -> float:
        return (self.total_samples if self.total_samples is not None else self.now_samples()) / RATE

    def name_of(self, uid: int) -> str:
        return self.names.get(uid, str(uid))

    def stop(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
        self._stop_evt.set()
        total = self.now_samples()
        self.total_samples = total
        for t in list(self.tracks.values()):
            t.finish(total)
        self._write_meta(total)

    def _flusher(self):
        ticks = 0
        every = max(1, round(SET["audio.meta_toutes_les_s"] / SET["audio.flush_s"]))
        while not self._stop_evt.wait(SET["audio.flush_s"]):
            for t in list(self.tracks.values()):
                t.flush()
            ticks += 1
            if ticks % every == 0:
                self._write_meta(-1)

    def _write_meta(self, total_samples: int):
        meta = {
            "sessionId": self.session_id,
            "startedAt": self.t0.isoformat(),
            "durationSamples": total_samples,
            "audio": {"format": "wav", "sampleRate": RATE, "channels": 1, "bitDepth": 16},
            "participants": [{"id": str(i), "name": n, "file": self.files.get(i)} for i, n in self.names.items()],
        }
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
