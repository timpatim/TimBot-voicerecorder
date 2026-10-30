"""Transcription en direct (best effort) : découpe en énoncés (VAD) puis WhisperX dans un thread séparé."""
import json
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .config import SET
from .constants import FRAME, FRAME_BYTES, RATE
from .utils import dbfs, fmt_ts
from .vad import HANGOVER, MAX_UTTERANCE, MIN_DBFS, MIN_VOICED_FRAMES, START_VOICED, TRAIL, VadState, make_vad_factory
from .whisper_engine import WhisperEngine


LIVE_DEBUG = SET["live.debug"]           # trace chaque énoncé (durée, niveau, décision)

# Phrases que Whisper « invente » sur du silence / du bruit (génériques de sous-titres, etc.)
_HALLUCINATION = re.compile(SET["whisper.filtre_hallucinations"], re.IGNORECASE)


def is_hallucination(text: str) -> bool:
    """Texte vide de sens : ponctuation seule ('...') ou formule de sous-titrage typique."""
    return not re.search(r"\w", text) or bool(_HALLUCINATION.search(text))


class LiveTranscriber:
    """Chemin temps réel, best effort.

    Thread VAD : reçoit les paquets de 20 ms, détecte la voix (webrtcvad), ignore les silences et
    concatène les trames voisées en énoncés. Thread ASR : transcrit chaque énoncé avec WhisperX et
    écrit des segments dont les timestamps sont sur la timeline de la session (donc cohérents entre
    utilisateurs et avec les fichiers audio). Si ça plante ou prend du retard, l'archive audio
    n'est pas touchée.
    """

    def __init__(self, engine: WhisperEngine, jsonl: Path, now_samples, name_of):
        self.engine = engine
        self.jsonl, self.now_samples, self.name_of = jsonl, now_samples, name_of
        self.recent: deque = deque(maxlen=SET["live.historique"])     # (début, texte formaté) pour la commande /transcript
        self._lock = threading.Lock()
        self.pending_count = 0                     # énoncés en attente (ou en cours) de transcription
        self.pending_samples = 0                   # ... et leur durée en échantillons
        self.done_count = 0
        self.audio_done = 0.0                      # secondes d'audio déjà transcrites
        self.busy = 0.0                            # secondes passées à les transcrire
        self.q: queue.Queue = queue.Queue()        # paquets venant du recorder
        self.asr_q: queue.Queue = queue.Queue()    # énoncés prêts à transcrire
        self.states: dict[int, VadState] = {}
        self.stopping = False
        self.dead = engine.failed
        self._vad_factory = make_vad_factory()
        self._vad_thread = threading.Thread(target=self._vad_loop, daemon=True, name="live-vad")
        self._asr_thread = threading.Thread(target=self._asr_loop, daemon=True, name="live-asr")

    # -- contrôle --

    def start(self):
        self._vad_thread.start()
        self._asr_thread.start()

    def stop(self):
        self.stopping = True

    def is_alive(self) -> bool:
        return self._vad_thread.is_alive() or self._asr_thread.is_alive()

    def join(self, timeout=None):
        self._vad_thread.join(timeout)
        self._asr_thread.join(timeout)

    def offer(self, uid: int, start: int, pcm: bytes):
        if not self.dead:
            self.q.put((uid, start, pcm))

    # -- thread VAD --

    def _vad_loop(self):
        while not (self.stopping and self.q.empty()):
            try:
                item = self.q.get(timeout=0.5)
            except queue.Empty:
                item = None
            if item:
                self._on_chunk(*item)
            if self.q.empty() and not self.stopping:
                self._idle(self.now_samples())
        for uid, st in self.states.items():
            if st.in_speech:
                self._end(uid, st, st.last_voice_end + TRAIL)
        self.asr_q.put(None)                       # fin de flux pour le thread ASR

    def _on_chunk(self, uid: int, start: int, pcm: bytes):
        st = self.states.get(uid)
        if st is None:
            st = self.states[uid] = VadState(self._vad_factory())
        if st.next is not None and start > st.next + FRAME // 2:
            self._gap(st, start)                   # pas de paquets pendant un moment = silence
            st.rem = b""
        data_start = start - len(st.rem) // 2
        data = st.rem + pcm
        n = len(data) // FRAME_BYTES
        for i in range(n):
            self._frame(uid, st, data_start + i * FRAME, data[i * FRAME_BYTES:(i + 1) * FRAME_BYTES])
        st.rem = data[n * FRAME_BYTES:]
        st.next = start + len(pcm) // 2

    def _gap(self, st: VadState, new_start: int):
        if st.in_speech:
            if new_start - st.last_voice_end >= HANGOVER:
                self._end(self._uid_of(st), st, st.last_voice_end + TRAIL)
            else:                                  # trou court : on comble pour garder la timeline exacte
                buf_end = st.start + len(st.buf) // 2
                st.buf += bytes(max(0, new_start - buf_end) * 2)
        else:
            st.pre.clear()
            st.ring.clear()

    def _uid_of(self, st: VadState) -> int:
        return next(u for u, s in self.states.items() if s is st)

    def _frame(self, uid: int, st: VadState, start: int, frame: bytes):
        voiced = st.vad(frame)
        end = start + FRAME
        if voiced:
            st.last_voice_end = end
        if not st.in_speech:
            st.pre.append((start, frame))
            st.ring.append(voiced)
            if sum(st.ring) >= START_VOICED:       # début de parole : on reprend le pré-roll
                st.in_speech = True
                st.start = st.pre[0][0]
                st.buf = bytearray(b"".join(f for _, f in st.pre))
                st.voiced = sum(st.ring)
            return
        st.buf += frame
        if voiced:
            st.voiced += 1
        if end - st.last_voice_end >= HANGOVER:    # 700 ms sans voix : fin d'énoncé
            self._end(uid, st, st.last_voice_end + TRAIL)
        elif end - st.start >= MAX_UTTERANCE:      # énoncé trop long : on coupe
            self._end(uid, st, end)

    def _idle(self, now: int):
        for uid, st in self.states.items():
            if st.in_speech and st.next is not None and now - st.next > HANGOVER:
                self._end(uid, st, min(st.next, st.last_voice_end + TRAIL))

    def _end(self, uid: int, st: VadState, end_sample: int):
        n = max(0, end_sample - st.start)
        pcm, start, voiced = bytes(st.buf[: n * 2]), st.start, st.voiced
        st.in_speech, st.buf, st.voiced = False, bytearray(), 0
        st.pre.clear()
        st.ring.clear()
        if len(pcm) < FRAME_BYTES * 5 or self.dead:
            return
        level = dbfs(pcm)
        info = (f"{self.name_of(uid)} {fmt_ts(start / RATE)} · {len(pcm) / 2 / RATE:.1f} s · "
                f"{level:.0f} dBFS · {voiced} trames voisées")
        if voiced < MIN_VOICED_FRAMES:
            if LIVE_DEBUG:
                print(f"[live] ignoré (trop peu de voix) : {info}")
            return
        if level < MIN_DBFS:           # le VAD a cru entendre de la voix, mais le signal est quasi plat
            print(f"[live] ignoré (trop faible, < {MIN_DBFS:.0f} dBFS) : {info}")
            return
        if LIVE_DEBUG:
            print(f"[live] envoyé à WhisperX : {info}")
        with self._lock:
            self.pending_count += 1
            self.pending_samples += len(pcm) // 2
        self.asr_q.put((uid, start, pcm))

    # -- thread ASR (WhisperX) --

    def _asr_loop(self):
        self._asr_run()

    def _asr_run(self):
        if not self.engine.wait_ready():          # attend le modèle (chargé au démarrage du programme)
            self.dead = True
            return
        while True:
            item = self.asr_q.get()
            if item is None:
                break
            t = time.monotonic()
            try:
                self._transcribe(*item)
            except Exception as e:
                print(f"[live] transcription échouée : {e!r}")
            finally:
                dt = time.monotonic() - t
                with self._lock:
                    self.pending_count -= 1
                    self.pending_samples -= len(item[2]) // 2
                    self.done_count += 1
                    self.audio_done += len(item[2]) / 2 / RATE
                    self.busy += dt

    # -- monitoring de la file d'attente ASR --

    def pending_seconds(self) -> float:
        with self._lock:
            return self.pending_samples / RATE

    def status_line(self) -> str:
        """Une ligne lisible : barre de remplissage de la file, débit de WhisperX, temps de rattrapage."""
        if self.dead or self.engine.failed:
            return "ASR désactivé (WhisperX indisponible)"
        with self._lock:
            count, pend_s = self.pending_count, self.pending_samples / RATE
            rtf = self.audio_done / self.busy if self.busy > 0 else None
        if not self.engine.ready:
            return f"WhisperX {self.engine.state_text} · {count} énoncé(s) en attente"
        warn = SET["live.retard_alerte_s"]                     # secondes d'audio en attente = barre pleine
        width = 20
        filled = min(width, int(round(width * pend_s / warn)))
        parts = [f"ASR [{'█' * filled}{'░' * (width - filled)}] {pend_s:.0f} s d'audio en attente ({count} énoncé(s))"]
        if rtf:
            parts.append(f"débit ×{rtf:.1f} temps réel")
            if pend_s > 0:
                parts.append(f"~{pend_s / rtf:.0f} s pour tout traiter")
            if rtf < 1:
                parts.append("⚠ plus lent que la parole")
        vad_backlog = self.q.qsize()
        if vad_backlog > 200:
            parts.append(f"⚠ VAD en retard ({vad_backlog} paquets)")
        return ("⚠ " if pend_s > warn else "") + " · ".join(parts)

    def _transcribe(self, uid: int, start: int, pcm: bytes):
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        duration = len(audio) / RATE
        segs = self.engine.transcribe(audio)
        if not segs:
            return
        name, base = self.name_of(uid), start / RATE
        rows, rejected = [], []
        for s in segs:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            s0 = min(max(float(s.get("start", 0.0)), 0.0), duration)
            s1 = min(max(float(s.get("end", duration)), s0), duration)
            row = {"user": str(uid), "name": name, "start": round(base + s0, 3),
                   "end": round(base + s1, 3), "text": text, "source": "live"}
            (rejected if is_hallucination(text) else rows).append(row)
        if rejected:                   # gardés à part (rien n'est perdu), mais hors du transcript
            with open(self.jsonl.with_name("transcript_live_rejected.jsonl"), "a", encoding="utf-8") as f:
                for r in rejected:
                    f.write(json.dumps({**r, "reason": "hallucination probable"}, ensure_ascii=False) + "\n")
                    print(f"[live] rejeté (hallucination probable) {fmt_ts(r['start'])} {name} : {r['text']!r}")
        if not rows:
            return
        with open(self.jsonl, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.recent.append((rows[0]["start"],
                            f"`{fmt_ts(rows[0]['start'])[:8]}` **{name}** : " + " ".join(r["text"] for r in rows)))
