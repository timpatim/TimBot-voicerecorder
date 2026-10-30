"""Configuration commune des tests : config isolée, doublures de discord / torch / whisperx."""
import os
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# config.ini de test, isolé du vrai ; on neutralise les variables d'environnement historiques du poste
_TMP = Path(tempfile.mkdtemp(prefix="voicebot-tests-"))
os.environ["BOT_CONFIG"] = str(_TMP / "config.ini")
for _var in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "WHISPER_MODEL", "WHISPER_DEVICE", "WHISPER_COMPUTE", "WHISPER_LANG",
             "WHISPER_BATCH", "WHISPERX_ALIGN", "VAD_AGGRESSIVENESS", "VAD_RMS", "MIN_UTTERANCE_DBFS", "LIVE_DEBUG",
             "LIVE_STATUS_INTERVAL", "ASR_BACKLOG_WARN", "REPROCESS_CMD", "FFMPEG", "GOOGLE_CREDENTIALS",
             "GOOGLE_TOKEN", "DRIVE_ROOT_NAME"):
    os.environ.pop(_var, None)
sys.argv = sys.argv[:1]

try:                                    # discord.py + le fork voice_recv installés : on les utilise
    import discord  # noqa: F401
    from discord.ext import voice_recv  # noqa: F401
except Exception:                       # sinon, doublures
    import discord_stubs
    discord_stubs.install()


@pytest.fixture
def cfg_file():
    """Réinitialise config.ini aux valeurs par défaut avant le test et le renvoie."""
    from voicebot.config import SET, default_config_text
    path = Path(os.environ["BOT_CONFIG"])
    path.write_text(default_config_text(), encoding="utf-8")
    SET.reload()
    yield path
    path.write_text(default_config_text(), encoding="utf-8")
    SET.reload()


class FakeEngine:
    """Remplace WhisperEngine : renvoie des segments préparés à l'avance."""
    ready, failed, state_text = True, False, "prêt"

    def __init__(self, script=()):
        self.script = list(script)

    def wait_ready(self):
        return True

    def transcribe(self, audio):
        return self.script.pop(0) if self.script else []


@pytest.fixture
def fake_engine():
    return FakeEngine


@pytest.fixture
def fake_whisperx(monkeypatch):
    """Faux modules torch et whisperx : permet de tester WhisperEngine sans modèle."""
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    wx = types.ModuleType("whisperx")
    calls = {"script": []}

    class Model:
        def transcribe(self, audio, batch_size=1, language="fr"):
            return {"segments": calls["script"].pop(0) if calls["script"] else []}

    wx.load_model = lambda name, device, compute_type=None, language=None: (calls.update(loaded=(name, device, compute_type)), Model())[1]
    wx.load_align_model = lambda language_code, device: (object(), {})
    wx.align = lambda segs, m, meta, audio, device, return_char_alignments=False: {"segments": segs}
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "whisperx", wx)
    return calls


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False
