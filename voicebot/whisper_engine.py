"""Moteur WhisperX : chargé une fois au démarrage du programme et partagé entre les sessions."""
import threading
import time

import numpy as np

from .config import SET


class WhisperEngine:
    """Charge WhisperX UNE fois, dès le démarrage du programme, et le partage entre les sessions."""

    STATE_TEXT = {"idle": "en attente", "loading": "chargement du modèle…",
                  "loading_align": "chargement de l'alignement…", "ready": "prêt",
                  "failed": "indisponible"}

    def __init__(self):
        self.state = "idle"            # idle | loading | loading_align | ready | failed
        self.error = None
        self.device = self.lang = self.batch = self.whisperx = None
        self.model = self.align_model = self.align_meta = None
        self._done = threading.Event()         # chargement terminé (succès ou échec)
        self._lock = threading.Lock()          # un seul calcul à la fois sur le modèle

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def failed(self) -> bool:
        return self.state == "failed"

    @property
    def state_text(self) -> str:
        return self.STATE_TEXT[self.state]

    def start_loading(self):
        if self.state != "idle":
            return
        self.state = "loading"
        threading.Thread(target=self._load, daemon=True, name="whisper-load").start()

    def wait_ready(self) -> bool:
        self._done.wait()
        return self.ready

    def _load(self):
        t0 = time.monotonic()
        try:
            import torch
            import whisperx
            self.device = SET["whisper.device"] or ("cuda" if torch.cuda.is_available() else "cpu")
            compute = SET["whisper.calcul"] or ("float16" if self.device == "cuda" else "int8")
            self.lang = SET["whisper.langue"]
            self.batch = SET["whisper.batch"] or (8 if self.device == "cuda" else 1)
            name = SET["whisper.modele"]
            self.whisperx = whisperx
            print(f"[whisper] chargement de « {name} » sur {self.device} ({compute})…")
            self.model = whisperx.load_model(name, self.device, compute_type=compute, language=self.lang)
            if SET["whisper.alignement"]:                  # alignement mot à mot : timestamps plus précis
                self.state = "loading_align"
                try:
                    self.align_model, self.align_meta = whisperx.load_align_model(
                        language_code=self.lang, device=self.device)
                except Exception as e:
                    print(f"[whisper] alignement indisponible, timestamps de segment bruts : {e!r}")
            self.state = "ready"
            print(f"[whisper] prêt en {time.monotonic() - t0:.0f} s")
        except Exception as e:
            self.error = e
            self.state = "failed"
            print(f"[whisper] chargement impossible, transcription live désactivée : {e!r}")
        finally:
            self._done.set()

    def transcribe(self, audio: np.ndarray) -> list:
        """audio : float32 mono 16 kHz. Retourne les segments (alignés si possible)."""
        with self._lock:
            segs = self.model.transcribe(audio, batch_size=self.batch, language=self.lang).get("segments", [])
            if segs and self.align_model is not None:
                try:
                    segs = self.whisperx.align(segs, self.align_model, self.align_meta, audio, self.device,
                                               return_char_alignments=False).get("segments", segs)
                except Exception as e:
                    print(f"[live] alignement échoué : {e!r}")
            return segs
