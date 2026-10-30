import json

import numpy as np
import pytest

from conftest import FakeEngine, wait_until
from voicebot import live as live_mod
from voicebot.constants import FRAME, RATE
from voicebot.live import LiveTranscriber, is_hallucination
from voicebot.vad import _EnergyVad


@pytest.fixture
def energy_vad(monkeypatch):
    """VAD déterministe (par énergie) : les tests ne dépendent pas de webrtcvad."""
    def use(threshold):
        monkeypatch.setattr(live_mod, "make_vad_factory", lambda: (lambda: _EnergyVad(threshold)))
    return use


def feed(live, uid, t0, sec, amp, seed=0):
    pcm = np.random.default_rng(seed).normal(0, amp, int(sec * RATE)).astype("<i2").tobytes()
    start = int(t0 * RATE)
    for i in range(0, len(pcm), FRAME * 2):          # paquets de 20 ms, comme Discord
        live.offer(uid, start + i // 2, pcm[i:i + FRAME * 2])


def run(tmp_path, script, feeds):
    live = LiveTranscriber(FakeEngine(script), tmp_path / "t.jsonl", lambda: 0, lambda u: f"user{u}")
    live.start()
    for args in feeds:
        feed(live, *args)
    live.stop()
    live.join(10)
    path = tmp_path / "t.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return live, rows


def test_enonce_transcrit_avec_timestamps_de_session(tmp_path, energy_vad):
    energy_vad(400)
    script = [[{"text": " Ceci est un test.", "start": 0.2, "end": 1.2}],
              [{"text": " Et voilà.", "start": 0.1, "end": 0.9}]]
    _, rows = run(tmp_path, script, [(1, 2.0, 1.5, 3000), (1, 10.0, 1.5, 3000)])
    assert [r["text"] for r in rows] == ["Ceci est un test.", "Et voilà."]
    first, second = rows
    assert 1.5 <= first["start"] < first["end"] <= 4.0           # position réelle dans la session (≈ 2 s)
    assert 9.5 <= second["start"] < second["end"] <= 12.5        # ≈ 10 s : les silences ne sont pas comptés
    assert first["user"] == "1" and first["name"] == "user1" and first["source"] == "live"


def test_deux_intervenants_en_parallele(tmp_path, energy_vad):
    energy_vad(400)
    script = [[{"text": " Alice parle.", "start": 0.0, "end": 1.0}], [{"text": " Bob répond.", "start": 0.0, "end": 1.0}]]
    _, rows = run(tmp_path, script, [(1, 2.0, 1.2, 3000), (2, 2.5, 1.2, 3000, 1)])
    assert {r["name"] for r in rows} == {"user1", "user2"}


def test_silence_ignore_et_rien_n_est_envoye_a_whisper(tmp_path, energy_vad):
    energy_vad(400)
    live, rows = run(tmp_path, [[{"text": " ne doit pas apparaître", "start": 0, "end": 1}]], [(1, 1.0, 2.0, 30)])
    assert rows == []


def test_enonce_vu_comme_voix_mais_trop_faible_est_ignore(tmp_path, energy_vad, capsys):
    energy_vad(1)                                  # VAD ultra permissif : le garde-fou de niveau doit rattraper
    _, rows = run(tmp_path, [[{"text": " halluciné", "start": 0, "end": 1}]], [(1, 1.0, 1.5, 3)])
    assert rows == []
    assert "trop faible" in capsys.readouterr().out


def test_bruit_tres_court_est_ignore(tmp_path, energy_vad):
    energy_vad(400)
    _, rows = run(tmp_path, [[{"text": " clac", "start": 0, "end": 1}]], [(1, 1.0, 0.1, 3000)])
    assert rows == []


def test_hallucinations_rejetees_mais_conservees(tmp_path, energy_vad):
    energy_vad(400)
    script = [[{"text": " ...", "start": 0.0, "end": 0.06}, {"text": " Sous-titrage ST' 501", "start": 0.5, "end": 1.0},
               {"text": " Merci beaucoup.", "start": 1.0, "end": 1.4}]]
    _, rows = run(tmp_path, script, [(1, 2.0, 1.5, 3000)])
    assert [r["text"] for r in rows] == ["Merci beaucoup."]
    rejected = (tmp_path / "transcript_live_rejected.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rejected) == 2 and all(json.loads(r)["reason"] for r in rejected)


@pytest.mark.parametrize("text,expected", [
    ("...", True), ("  … ", True), ("!", True), ("Sous-titrage ST' 501", True),
    ("Sous-titres réalisés par la communauté d'Amara.org", True), ("Merci d’avoir regardé cette vidéo", True),
    ("Abonnez-vous", True), ("Merci beaucoup.", False), ("C'est tout.", False), ("Ok, je capte ça ?", False)])
def test_filtre_hallucinations(text, expected):
    assert is_hallucination(text) is expected


def test_ligne_de_statut_et_file_d_attente(tmp_path, energy_vad):
    energy_vad(400)
    live, _ = run(tmp_path, [[{"text": " ok", "start": 0, "end": 1}]], [(1, 1.0, 1.0, 3000)])
    assert live.pending_seconds() == 0 and "ASR [" in live.status_line()
    assert not live.is_alive()


def test_whisper_engine_charge_et_transcrit(fake_whisperx):
    from voicebot.whisper_engine import WhisperEngine
    e = WhisperEngine()
    assert e.state == "idle" and e.state_text == "en attente"
    e.start_loading()
    e.start_loading()                              # idempotent
    assert e.wait_ready() and e.ready and e.device == "cpu"
    fake_whisperx["script"].append([{"text": " salut", "start": 0.0, "end": 0.5}])
    assert e.transcribe(np.zeros(RATE, dtype="float32"))[0]["text"] == " salut"


def test_whisper_engine_echec_de_chargement(monkeypatch, fake_whisperx):
    import sys
    sys.modules["whisperx"].load_model = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CUDA out of memory"))
    from voicebot.whisper_engine import WhisperEngine
    e = WhisperEngine()
    e.start_loading()
    assert not e.wait_ready() and e.failed and "CUDA" in str(e.error)
    assert wait_until(lambda: e.state == "failed")
