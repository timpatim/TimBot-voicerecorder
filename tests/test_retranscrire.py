"""Retranscription d'une session depuis ses WAV complets (voicebot/retranscrire.py et commande /retranscrire)."""
import asyncio
import json
import types
from datetime import datetime

import pytest

from voicebot import commands, retranscrire
from voicebot.config import SET
from tests.test_commands import build
from tests.test_rejeu import FakeEngine, session, write_wav  # noqa: F401  (fixture `session` : un joueur, deux phrases)


def old_transcript(d):
    rows = [{"user": "1", "name": "Anna", "start": 1.0, "end": 2.0, "text": "Ancien texte.", "source": "live"}]
    (d / "transcript_live.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (d / "transcript_live.txt").write_text("ancien txt\n", encoding="utf-8")


def test_sessions_avec_audio_les_plus_recentes_d_abord(tmp_path):
    for name, audio in (("2026-10-01_200000", True), ("2026-11-03_214508", True), ("2026-11-05_000000", False)):
        (tmp_path / name / "audio").mkdir(parents=True)
        if audio:
            write_wav(tmp_path / name / "audio" / "A.wav", [(1, 0)])
    assert retranscrire.sessions_with_audio(tmp_path) == ["2026-11-03_214508", "2026-10-01_200000"]
    assert retranscrire.sessions_with_audio(tmp_path / "absent") == []


def test_retranscription_remplace_le_transcript_et_garde_l_ancien(session):
    old_transcript(session)
    seen = []
    out = retranscrire.retranscribe(session, FakeEngine(), seen.append, now=datetime(2026, 11, 4, 23, 30, 0))
    rows = [json.loads(x) for x in (session / "transcript_live.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and all(r["text"].startswith("Phrase de ") for r in rows)
    assert all(r["source"] == "rejeu" and r["user"] == "1" and r["name"] == "Anna" for r in rows)
    assert out["lignes"] == 2 and out["ancien_nb_lignes"] == 1 and out["mots"] > 0
    # l'ancien transcript est conservé tel quel
    saved = session / "avant_retranscription_20261104_233000"
    assert out["sauvegarde"] == saved
    assert "Ancien texte." in (saved / "transcript_live.jsonl").read_text(encoding="utf-8")
    assert (saved / "transcript_live.txt").read_text(encoding="utf-8") == "ancien txt\n"
    # les fichiers dérivés sont refaits depuis le nouveau transcript
    assert "Phrase de " in (session / "transcript_live.txt").read_text(encoding="utf-8")
    assert (session / "transcript_nettoye.txt").exists()
    stats = json.loads((session / "stats.json").read_text(encoding="utf-8"))
    assert stats["lines"] == 2 and (session / "stats.txt").exists()
    assert any(t.startswith("Écriture") for t in seen) and any("énoncé" in t for t in seen)


def test_echec_de_whisper_ne_modifie_rien(session):
    old_transcript(session)
    before = (session / "transcript_live.jsonl").read_text(encoding="utf-8")

    class Boom(FakeEngine):
        def transcribe(self, audio):
            raise RuntimeError("GPU hors service")

    with pytest.raises(RuntimeError, match="GPU"):
        retranscrire.retranscribe(session, Boom())
    assert (session / "transcript_live.jsonl").read_text(encoding="utf-8") == before
    assert not list(session.glob("avant_retranscription_*"))


def test_session_sans_transcript_precedent_pas_de_sauvegarde(session):
    out = retranscrire.retranscribe(session, FakeEngine())
    assert out["sauvegarde"] is None and out["ancien_nb_lignes"] == 0 and out["lignes"] == 2


def test_aucun_fichier_audio(tmp_path):
    (tmp_path / "audio").mkdir()
    with pytest.raises(retranscrire.RetranscribeError, match="Aucun fichier audio"):
        retranscrire.retranscribe(tmp_path, FakeEngine())


# --- la commande ------------------------------------------------------------------------------------

class Msg:
    async def edit(self, **kw): pass


class Chan:
    id = 5
    mention = "<#5>"

    def __init__(self): self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})
        return Msg()


class Inter:
    def __init__(self, perm=True):
        self.user = types.SimpleNamespace(id=1, display_name="Couette", guild_permissions=types.SimpleNamespace(manage_guild=perm))
        self.guild, self.guild_id, self.channel_id, self.channel = types.SimpleNamespace(get_channel=lambda i: None, me=None), 7, 5, Chan()
        self.out, self.follow, self._done = [], [], False
        self.response = types.SimpleNamespace(send_message=self._send, is_done=lambda: self._done, defer=self._defer)
        self.followup = types.SimpleNamespace(send=self._follow)

    async def _send(self, content=None, **kw): self.out.append(content); self._done = True
    async def _defer(self, **kw): self._done = True

    async def _follow(self, content=None, **kw):
        self.follow.append(content)
        return Msg()


def prepared(session, monkeypatch):
    bot, engine = build()
    engine.state = "ready"
    engine._done.set()                                  # (le chargement est « terminé » : wait_ready ne bloque pas)
    engine.transcribe = FakeEngine().transcribe
    monkeypatch.setitem(SET.values, "sessions.dossier", str(session.parent))
    monkeypatch.setitem(SET.values, "discord.canal_messages", "")
    commands._RETRANSCRIBING.clear()
    return bot, engine


def run(bot, inter, **kw):
    asyncio.run(bot.tree.commands["retranscrire"](inter, **kw))


def test_commande_retranscrit_la_derniere_session(session, monkeypatch):
    old_transcript(session)
    bot, _ = prepared(session, monkeypatch)
    inter = Inter()
    run(bot, inter)
    assert "lancée" in inter.follow[0] and "ancien transcript sera conservé" in inter.follow[0]
    final = inter.channel.sent[-1]
    assert "retranscrite" in final["content"] and "2 lignes (avant : 1)" in final["content"]
    assert "avant_retranscription_" in final["content"] and final["embed"] is not None
    assert (session / "transcript_live.jsonl").read_text(encoding="utf-8").count("rejeu") == 2
    assert not commands._RETRANSCRIBING


def test_commande_refusee_dans_les_cas_non_permis(session, monkeypatch):
    bot, engine = prepared(session, monkeypatch)
    inter = Inter(perm=False)
    run(bot, inter)
    assert "Gérer le serveur" in inter.out[-1]
    inter = Inter()
    run(bot, inter, session="1999-01-01")
    assert "introuvable" in inter.out[-1]
    bot.sessions[7] = object()
    run(bot, inter)
    assert "enregistrement est en cours" in inter.out[-1]
    bot.sessions.clear()
    commands._RETRANSCRIBING.add("autre")
    run(bot, inter)
    assert "déjà en cours" in inter.out[-1] and "autre" in inter.out[-1]
    commands._RETRANSCRIBING.clear()
    engine.state, engine.error = "failed", RuntimeError("cuda")
    run(bot, inter)
    assert "indisponible" in inter.out[-1]
    assert not (session / "stats.json").exists()


def test_commande_echec_message_clair_et_etat_libere(session, monkeypatch):
    old_transcript(session)
    bot, engine = prepared(session, monkeypatch)

    def boom(audio): raise RuntimeError("mémoire GPU saturée")

    engine.transcribe = boom
    inter = Inter()
    run(bot, inter)
    final = inter.channel.sent[-1]["content"]
    assert "impossible" in final and "mémoire GPU saturée" in final and "n'a pas été modifié" in final
    assert "Ancien texte." in (session / "transcript_live.jsonl").read_text(encoding="utf-8")
    assert not commands._RETRANSCRIBING


def test_record_et_voix_refuses_pendant_une_retranscription(session, monkeypatch):
    bot, _ = prepared(session, monkeypatch)
    commands._RETRANSCRIBING.add("2026-11-03_214508")
    try:
        assert commands.audio_busy()
        inter = Inter()
        inter.user.voice = types.SimpleNamespace(channel=object())
        asyncio.run(bot.tree.commands["record"](inter))
        assert "retranscription est en cours" in inter.out[-1]
    finally:
        commands._RETRANSCRIBING.clear()
