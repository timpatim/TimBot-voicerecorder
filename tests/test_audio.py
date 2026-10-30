import wave

import numpy as np

from voicebot.audio import GAP_TOLERANCE, SessionRecorder, Track
from voicebot.constants import RATE


def tone(sec, amp=2500):
    return np.random.default_rng(0).normal(0, amp, int(sec * RATE)).astype("<i2").tobytes()


def test_wav_lisible_pendant_l_ecriture_et_comble_les_silences(tmp_path):
    path = tmp_path / "x.wav"
    t = Track(path)
    pcm = b"\x01\x00" * 320                      # 20 ms
    t.write(pcm, 0)
    t.write(pcm, 320)
    t.write(pcm, 20_000)                         # long silence : comblé avec des zéros
    t.flush()
    with wave.open(str(path)) as w:              # en-tête valide AVANT la fin
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2)
        assert w.getnframes() == 20_320
    t.finish(30_000)                             # tous les fichiers finissent à la même durée
    with wave.open(str(path)) as w:
        assert w.getnframes() == 30_000


def test_petite_gigue_ne_cree_pas_de_silence(tmp_path):
    t = Track(tmp_path / "x.wav")
    pcm = b"\x01\x00" * 320
    t.write(pcm, 0)
    t.write(pcm, 320 + GAP_TOLERANCE // 2)
    t.finish(640)
    with wave.open(str(tmp_path / "x.wav")) as w:
        assert w.getnframes() == 640


def test_noms_de_fichiers(tmp_path):
    rec = SessionRecorder("s", tmp_path)
    try:
        assert rec._filename(1, 'Tim: <le>/boss?') == "Tim_ _le__boss_.wav"
        assert rec._filename(2, "   ") == "2.wav"
        rec.files = {1: "Tim.wav"}
        assert rec._filename(2, "Tim") == "Tim_2.wav"          # deux utilisateurs au même nom
    finally:
        rec.stop()


def test_arret_complete_les_pistes_et_ecrit_meta(tmp_path):
    rec = SessionRecorder("s", tmp_path)
    rec.names[7] = "Zoé"
    rec.files[7] = "Zoé.wav"
    rec.tracks[7] = Track(rec.dir / "audio" / "Zoé.wav")
    rec.tracks[7].write(tone(1), 0)
    rec.stop()
    assert rec.total_samples is not None and rec.duration_s() > 0
    with wave.open(str(rec.dir / "audio" / "Zoé.wav")) as w:       # jamais tronquée : au moins la durée de session
        assert w.getnframes() == max(rec.total_samples, RATE)
    assert (rec.dir / "meta.json").exists()
