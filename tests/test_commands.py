"""Assemblage du bot comme dans bot_v7.py : toutes les commandes sont déclarées, le statut est cohérent."""
from voicebot.client import RecorderBot
from voicebot.commands import CAMPAGNE_ACTIONS, GLOSSAIRE_ACTIONS, register
from voicebot.drive import DriveClient
from voicebot.whisper_engine import WhisperEngine


def build():
    engine, drive = WhisperEngine(), DriveClient()
    bot = RecorderBot(engine)
    register(bot, engine, drive)
    return bot, engine


def test_commandes_declarees():
    bot, _ = build()
    assert set(bot.tree.commands) == {"record", "stop", "transcript", "regle", "campagne", "stats", "status", "config", "dicter", "retranscrire", "glossaire"}
    assert list(bot.tree.commands["campagne"].subcommands) == CAMPAGNE_ACTIONS
    assert list(bot.tree.commands["glossaire"].subcommands) == GLOSSAIRE_ACTIONS
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


def test_limites_discord_sur_les_descriptions():
    """Discord refuse la synchronisation si une description dépasse 100 caractères (ou un nom 32)."""
    bot, _ = build()
    items = dict(bot.tree.commands)
    for group in [c for c in bot.tree.commands.values() if hasattr(c, "subcommands")]:
        items.update({f"{group.name} {sub}": fn for sub, fn in group.subcommands.items()})
    for name, fn in items.items():
        assert len(name.split()[-1]) <= 32 and 1 <= len(fn.description) <= 100, name
        for param, text in getattr(fn, "param_descriptions", {}).items():
            assert 1 <= len(text) <= 100, f"/{name} {param} : {len(text)} caractères"
