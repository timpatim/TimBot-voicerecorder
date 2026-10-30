import asyncio
import json
import re
import shutil
import types

import numpy as np
import pytest

from voicebot.audio import SessionRecorder, Track
from voicebot.config import load_config
from voicebot.constants import RATE
from voicebot.drive import DriveClient
from voicebot.pipeline import PIPELINE_STEPS, StatusBoard, run_pipeline

pytest.importorskip("reportlab")


class FakeLive:
    dead = False

    def stop(self): pass
    def join(self, timeout=None): pass
    def is_alive(self): return False
    def pending_seconds(self): return 0.0


class FakeDrive:
    """Simule la chaîne de l'API Google Drive (dossiers, envoi par blocs, permissions)."""

    def __init__(self):
        self.uploaded, self.permissions, self.folders = [], [], {}
        drive = self

        class Req:
            def __init__(self, fn): self.fn = fn
            def execute(self, num_retries=0): return self.fn()

        class Upload:
            def __init__(self, body): self.body, self.calls = body, 0

            def next_chunk(self, num_retries=0):
                self.calls += 1
                if self.calls == 1:
                    return types.SimpleNamespace(progress=lambda: 0.5), None
                drive.uploaded.append(self.body)
                return None, {"id": "f"}

        class Files:
            def list(self, q, spaces, fields, pageSize):
                name = re.search(r"name='([^']*)'", q).group(1)
                return Req(lambda: {"files": [{"id": drive.folders[name]}] if name in drive.folders else []})

            def create(self, body, fields, media_body=None):
                if media_body is None:
                    def make():
                        drive.folders[body["name"]] = "dir_" + body["name"]
                        return {"id": drive.folders[body["name"]]}
                    return Req(make)
                return Upload(body)

            def get(self, fileId, fields):
                return Req(lambda: {"webViewLink": "https://drive.google.com/drive/folders/" + fileId})

        class Permissions:
            def create(self, fileId, body, fields):
                drive.permissions.append(body)
                return Req(lambda: {"id": "p"})

        self.service = types.SimpleNamespace(files=lambda: Files(), permissions=lambda: Permissions())


@pytest.fixture
def drive(monkeypatch):
    http = types.ModuleType("googleapiclient.http")
    http.MediaFileUpload = lambda path, **kw: ("media", path)
    monkeypatch.setitem(__import__("sys").modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(__import__("sys").modules, "googleapiclient.http", http)
    fake = FakeDrive()
    client = DriveClient()
    client.service, client.state = fake.service, "ready"
    client._done.set()
    client.fake = fake
    return client


@pytest.fixture
def session(tmp_path):
    rec = SessionRecorder("2026-10-30_120000", tmp_path / "sessions")
    rng = np.random.default_rng(4)
    for uid, name in ((11, "timpatim"), (22, "Zoé")):
        rec.names[uid] = name
        fname = rec._filename(uid, name)
        rec.files[uid] = fname
        track = rec.tracks[uid] = Track(rec.dir / "audio" / fname)
        track.write(rng.normal(0, 2500, RATE * 10).astype("<i2").tobytes(), 0)
    rows, t = [], 1.0
    for i in range(8):
        u = [("11", "timpatim"), ("22", "Zoé")][i % 2]
        rows.append({"user": u[0], "name": u[1], "start": t, "end": t + 3, "text": "On avance sur le projet, bien ?", "source": "live"})
        t += 3.5
    (rec.dir / "transcript_live.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return rec


class Msg:
    def __init__(self): self.versions = []
    async def edit(self, content, allowed_mentions=None): self.versions.append(content)


class Channel:
    async def send(self, content, allowed_mentions=None):
        self.msg = Msg()
        self.msg.versions.append(content)
        return self.msg


def process(rec, cfg, drive):
    async def main():
        channel = Channel()
        board = StatusBoard(channel, asyncio.get_running_loop(), "**Traitement**", PIPELINE_STEPS)
        for key in ("mp3", "rapport", "drive"):
            if not cfg[key]:
                board.state[key] = ("skipped", "désactivé")
        await board.start()
        res = await asyncio.to_thread(run_pipeline, rec, FakeLive(), cfg, board, drive)
        await board.refresh()
        return res, channel.msg.versions[-1]
    return asyncio.run(main())


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent")


@needs_ffmpeg
def test_traitement_complet(cfg_file, session, drive):
    res, board = process(session, load_config(), drive)
    assert board.count("✅") == 5, board
    assert res["link"].endswith("dir_2026-10-30_120000") and res["shared"] is True
    names = [b["name"] for b in drive.fake.uploaded]
    assert sorted(names) == sorted(["transcript_live.txt", "rapport.pdf", "timpatim.mp3", "Zoé.mp3"])
    assert drive.fake.permissions == [{"type": "anyone", "role": "reader"}]
    assert (session.dir / "stats.json").exists() and res["report"].read_bytes().startswith(b"%PDF")
    assert all(p.stat().st_size > 1000 for p in res["mp3"])


def test_etapes_desactivees_dans_config_ini(cfg_file, session, drive):
    text = cfg_file.read_text(encoding="utf-8")
    for old, new in (("mp3 = oui", "mp3 = non"), ("rapport = oui", "rapport = non"),
                     ("drive_audio = oui", "drive_audio = non"), ("partage = lien", "partage = aucun")):
        text = text.replace(old, new, 1)
    cfg_file.write_text(text, encoding="utf-8")
    res, board = process(session, load_config(), drive)
    assert board.count("⏭️") == 2 and "Google Drive — 1 fichier(s)" in board
    assert [b["name"] for b in drive.fake.uploaded] == ["transcript_live.txt"]
    assert drive.fake.permissions == [] and res["shared"] is False


def test_sans_drive(cfg_file, session, drive):
    cfg = load_config()
    cfg.update(mp3=False, drive=False)
    res, _ = process(session, cfg, drive)
    assert res["link"] is None and drive.fake.uploaded == [] and res["report"].exists()


def test_echec_d_une_etape_n_arrete_pas_les_suivantes(cfg_file, session, drive, monkeypatch):
    import voicebot.pipeline as pipeline
    monkeypatch.setattr(pipeline, "build_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PDF cassé")))
    cfg = load_config()
    cfg["mp3"] = False
    res, board = process(session, cfg, drive)
    assert "❌ Rapport PDF — PDF cassé" in board and "✅ Google Drive" in board
    assert [b["name"] for b in drive.fake.uploaded].count("transcript_live.txt") == 1


@needs_ffmpeg
def test_mp3_echec_si_ffmpeg_introuvable(cfg_file, session, monkeypatch):
    from voicebot.config import SET
    from voicebot.mp3 import convert_to_mp3
    monkeypatch.setitem(SET.values, "mp3.ffmpeg", "ffmpeg-qui-n-existe-pas")
    with pytest.raises(RuntimeError, match="ffmpeg introuvable"):
        convert_to_mp3(session, 64)


def test_drive_non_configure(tmp_path, monkeypatch):
    from voicebot.config import SET
    monkeypatch.setitem(SET.values, "drive.identifiants", str(tmp_path / "absent.json"))
    monkeypatch.setitem(SET.values, "drive.jeton", str(tmp_path / "absent_token.json"))
    client = DriveClient()
    client.start()
    assert not client.wait_ready(timeout=5) and client.state == "not_configured"
    assert "introuvable" in client.problem()
