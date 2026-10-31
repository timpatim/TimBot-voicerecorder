import asyncio
import json
import sys
import types

import pytest

from voicebot import rules
from voicebot.audio import SessionRecorder
from voicebot.config import SET


class FakeGenai:
    """Doublure du SDK google-genai : enregistre l'appel reçu."""

    def __init__(self, monkeypatch, text="La règle est la suivante.", error=None):
        self.calls, self.client_args = [], {}
        outer = self

        class Models:
            def generate_content(self, model, contents, config):
                outer.calls.append({"model": model, "contents": contents, "config": config})
                if error:
                    raise error
                return types.SimpleNamespace(text=text)

        class Client:
            def __init__(self, api_key):
                outer.client_args["api_key"] = api_key
                self.models = Models()

        ns = lambda **kw: types.SimpleNamespace(**kw)           # noqa: E731
        t = types.ModuleType("google.genai.types")
        t.GenerateContentConfig = lambda **kw: ns(**kw)
        t.HttpOptions = lambda **kw: ns(**kw)
        t.Tool = lambda **kw: ns(**kw)
        t.ThinkingConfig = lambda **kw: ns(**kw)
        t.GoogleSearch = lambda: "google_search"
        g = types.ModuleType("google.genai")
        g.Client, g.types = Client, t
        pkg = types.ModuleType("google")
        pkg.genai, pkg.__path__ = g, []
        for name, mod in (("google", pkg), ("google.genai", g), ("google.genai.types", t)):
            monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setitem(SET.values, "regles.cle_api", "cle-de-test")


ROWS = [{"name": "Zoé", "start": 12.0, "text": " Non, le jet de sauvegarde est de Réflexes."},
        {"name": "Tim", "start": 5.0, "text": "Le sort fait des dégâts de zone, on fait quoi ?"},
        {"name": "Tim", "start": 20.0, "text": "   "},
        {"name": "Léo", "start": 1.0, "text": "Ancienne phrase"}]


def test_dernieres_phrases_dans_l_ordre(cfg_file):
    assert rules.transcript_lines(ROWS, 2) == ["Tim : Le sort fait des dégâts de zone, on fait quoi ?",
                                               "Zoé : Non, le jet de sauvegarde est de Réflexes."]


def test_jeu_par_defaut_et_consigne(cfg_file):
    assert SET["regles.jeu_par_defaut"] == "Pathfinder première édition"
    assert "« Pathfinder première édition »" in rules.build_instruction("Pathfinder première édition")
    assert "{jeu}" not in rules.build_instruction("D&D 5e")


def test_appel_gemini(cfg_file, key, monkeypatch):
    fake = FakeGenai(monkeypatch)
    out = rules.explain_rule(["Tim : on fait quoi ?"], "Pathfinder première édition")
    assert out == "La règle est la suivante." and fake.client_args == {"api_key": "cle-de-test"}
    call = fake.calls[0]
    assert call["model"] == SET["regles.modele"] and "Tim : on fait quoi ?" in call["contents"]
    assert "Pathfinder première édition" in call["config"].system_instruction
    assert call["config"].tools is None and call["config"].http_options.timeout == 60_000


def test_recherche_web_optionnelle(cfg_file, key, monkeypatch):
    fake = FakeGenai(monkeypatch)
    monkeypatch.setitem(SET.values, "regles.recherche_web", True)
    rules.explain_rule(["a : b"], "X")
    assert len(fake.calls[0]["config"].tools) == 1


def test_erreurs_lisibles(cfg_file, key, monkeypatch):
    FakeGenai(monkeypatch, error=RuntimeError("429 quota dépassé"))
    with pytest.raises(rules.RulesError, match="429 quota"):
        rules.explain_rule(["a : b"], "X")
    FakeGenai(monkeypatch, text="  ")
    with pytest.raises(rules.RulesError, match="aucune réponse"):
        rules.explain_rule(["a : b"], "X")
    with pytest.raises(rules.RulesError, match="Aucune phrase"):
        rules.explain_rule([], "X")


def test_sans_cle(cfg_file, monkeypatch):
    monkeypatch.setitem(SET.values, "regles.cle_api", "")
    with pytest.raises(rules.RulesError, match="GEMINI_API_KEY"):
        rules.explain_rule(["a : b"], "X")


def test_cle_lue_dans_l_environnement(tmp_path, monkeypatch):
    from voicebot.config import Settings
    monkeypatch.setenv("GEMINI_API_KEY", "depuis-env")
    assert Settings(tmp_path / "c.ini")["regles.cle_api"] == "depuis-env"


# ---- la commande Discord, avec une interaction factice ----

class FakeInter:
    def __init__(self):
        self.guild_id, self.sent, self.follow = 1, [], []
        self.response = types.SimpleNamespace(send_message=self._send, defer=self._defer, is_done=lambda: hasattr(self, "deferred"))
        self.followup = types.SimpleNamespace(send=self._follow)

    async def _send(self, content=None, **kw): self.sent.append((content, kw))
    async def _defer(self, **kw): self.deferred = True
    async def _follow(self, content=None, **kw): self.follow.append((content, kw))


@pytest.fixture
def bot_with_session(cfg_file, tmp_path, fake_engine):
    from voicebot.client import RecorderBot
    from voicebot.commands import register
    from voicebot.drive import DriveClient
    bot = RecorderBot(fake_engine())
    register(bot, fake_engine(), DriveClient())
    rec = SessionRecorder("s", tmp_path)
    rec.stop()
    (rec.dir / "transcript_live.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS), encoding="utf-8")
    live = types.SimpleNamespace(status_line=lambda: "ASR [ok]")
    bot.sessions[1] = (rec, live, None)
    return bot


def run(bot, **kwargs):
    inter = FakeInter()
    asyncio.run(bot.tree.commands["regle"](inter, **kwargs))
    return inter


def test_commande_regle(bot_with_session, key, monkeypatch):
    fake = FakeGenai(monkeypatch, text="Jet de Réflexes, demi-dégâts en cas de réussite.")
    inter = run(bot_with_session, jeu="D&D 5e", phrases=2)
    embed = inter.follow[0][1]["embed"]
    assert "D&D 5e" in embed.title and "Réflexes" in embed.description
    assert "Zoé : Non" in embed.fields[0]["value"] and "Léo" not in embed.fields[0]["value"]
    assert "D&D 5e" in fake.calls[0]["config"].system_instruction


def test_commande_jeu_par_defaut(bot_with_session, key, monkeypatch):
    fake = FakeGenai(monkeypatch)
    run(bot_with_session)
    assert "Pathfinder première édition" in fake.calls[0]["config"].system_instruction


def test_commande_anti_rafale(bot_with_session, key, monkeypatch):
    FakeGenai(monkeypatch)
    bot = bot_with_session
    run(bot)
    second = run(bot)
    assert "Patiente" in second.sent[0][0] and second.sent[0][1]["ephemeral"] is True


def test_commande_erreur_gemini_reautorise_un_nouvel_essai(bot_with_session, key, monkeypatch):
    FakeGenai(monkeypatch, error=RuntimeError("panne"))
    bot = bot_with_session
    assert "❌" in run(bot).follow[0][0]
    FakeGenai(monkeypatch)
    assert "embed" in run(bot).follow[0][1]


def test_commande_cas_particuliers(bot_with_session, cfg_file, monkeypatch):
    bot = bot_with_session
    monkeypatch.setitem(SET.values, "regles.cle_api", "")
    assert "GEMINI_API_KEY" in run(bot).sent[0][0]
    monkeypatch.setitem(SET.values, "regles.cle_api", "k")
    monkeypatch.setitem(SET.values, "regles.activer", False)
    assert "désactivée" in run(bot).sent[0][0]
    monkeypatch.setitem(SET.values, "regles.activer", True)
    del bot.sessions[1]
    assert "Aucun enregistrement" in run(bot).sent[0][0]


def test_reflexion_rapide_par_defaut_et_desactivable(cfg_file, key, monkeypatch):
    fake = FakeGenai(monkeypatch)
    rules.explain_rule(["a : b"], "X")
    assert fake.calls[0]["config"].thinking_config.thinking_level == "low"
    monkeypatch.setitem(SET.values, "regles.reflexion", "auto")
    rules.explain_rule(["a : b"], "X")
    assert fake.calls[1]["config"].thinking_config is None


def test_conseil_en_cas_de_delai_depasse(cfg_file, key, monkeypatch):
    FakeGenai(monkeypatch, error=RuntimeError("504 DEADLINE_EXCEEDED. Deadline expired"))
    with pytest.raises(rules.RulesError) as e:
        rules.explain_rule(["a : b"], "X")
    assert "reflexion" in str(e.value) and "delai_attente_s" in str(e.value)


def test_reflexion_valeur_invalide():
    from voicebot.config import parse_setting
    with pytest.raises(ValueError):
        parse_setting("regles.reflexion", "turbo")
    assert parse_setting("regles.reflexion", "LOW") == "low"
