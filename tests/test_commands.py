"""Assemblage du bot comme dans bot_v4.py : toutes les commandes sont déclarées, le statut est cohérent."""
from voicebot.client import RecorderBot
from voicebot.commands import register
from voicebot.drive import DriveClient
from voicebot.whisper_engine import WhisperEngine


def build():
    engine, drive = WhisperEngine(), DriveClient()
    bot = RecorderBot(engine)
    register(bot, engine, drive)
    return bot, engine


def test_commandes_declarees():
    bot, _ = build()
    assert set(bot.tree.commands) == {"record", "stop", "transcript", "regle", "stats", "status", "config"}
    assert bot.tree.error_handler is not None


def test_statut_de_presence_suit_l_etat_de_whisper():
    bot, engine = build()
    assert bot._presence()[1].startswith("⏳")
    engine.state = "ready"
    assert bot._presence() == ("online", "✅ Whisper prêt · /record pour démarrer")
    engine.state = "failed"
    assert bot._presence()[0] == "dnd"


def test_close_all_sans_session():
    bot, _ = build()
    bot.close_all()
