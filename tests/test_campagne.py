"""Campagnes : store, joueurs.json, génération du contexte (Gemini simulé) et commande /campagne."""
import asyncio
import json
import types

import pytest

from voicebot import campagne, commands
from voicebot.config import SET

MJ = {"id": 1, "name": "Couette", "username": "couette"}
P1 = {"id": 2, "name": "Silent", "username": "silentgob"}
P2 = {"id": 3, "name": "Raph", "username": "raph9630"}

REPONSE = """=== CONTEXTE ===
# Campagne : Hell's Rebels
## Personnages joueurs
### Max — joué par Silent
- Vrai nom : Filbester [secret:Max]
=== JOUEURS ===
```json
[{"pseudo": "silentgob", "personnage": "Max"}, {"pseudo": "inconnu", "personnage": "X"}]
```"""


@pytest.fixture
def store(tmp_path, monkeypatch, cfg_file):
    monkeypatch.setitem(SET.values, "campagnes.dossier", str(tmp_path / "camp"))
    return campagne.CampaignStore(tmp_path / "camp")


def test_slugify():
    assert campagne.slugify("Hell's Rebels !") == "hell-s-rebels"
    assert campagne.slugify("Été à Kéré") == "ete-a-kere"


def test_creation_prefill_joueurs_et_activation(store):
    c = store.create("Hell's Rebels", "Pathfinder 1e", MJ, [MJ, P1, P2])
    data = c.joueurs()
    assert data["mj"]["discord_id"] == "1" and data["mj"]["pseudos"] == ["Couette"]
    assert [j["joueur"] for j in data["joueurs"]] == ["Silent", "Raph"]      # le MJ n'est pas joueur
    sans = store.create("Sans MJ", "x", None, [MJ, P1])
    assert sans.joueurs()["mj"]["discord_id"] == "" and len(sans.joueurs()["joueurs"]) == 2
    campagne.set_mj(sans, MJ)
    assert sans.joueurs()["mj"]["discord_id"] == "1" and [j["joueur"] for j in sans.joueurs()["joueurs"]] == ["Silent"]
    assert all(j["personnage"] == "" for j in data["joueurs"])
    assert not c.contexte_rempli() and campagne.TEMPLATE_MARK in c.contexte_path.read_text(encoding="utf-8")
    assert store.active().slug == "sans-mj"        # la dernière créée devient active
    with pytest.raises(campagne.CampaignError):
        store.create("Hell's Rebels", "x", MJ, [])


def test_choix_par_prefixe_et_ambiguite(store):
    store.create("Alpha un", "x", MJ, [])
    store.create("Alpha deux", "x", MJ, [])
    store.create("Beta", "x", MJ, [])
    assert store.get("bet").titre == "Beta"
    assert store.get("Alpha un").slug == "alpha-un"
    with pytest.raises(campagne.CampaignError, match="ambigu"):
        store.get("alp")
    with pytest.raises(campagne.CampaignError, match="introuvable"):
        store.get("zzz")
    store.set_active(store.get("alpha-deux"))
    assert store.active().slug == "alpha-deux"


def test_contexte_genere_remplit_template_et_personnages(store, monkeypatch):
    c = store.create("Hell's Rebels", "Pathfinder 1e", MJ, [MJ, P1, P2])
    seen = {}
    monkeypatch.setattr(campagne.gemini, "generate",
                        lambda ins, contents, **kw: seen.update(ins=ins, contents=contents) or REPONSE)
    res = campagne.generate_contexte(c, "texte libre")
    assert res == {"fichier": "contexte.md", "remplis": 1, "total": 2}
    assert "[secret:Max]" in c.contexte_path.read_text(encoding="utf-8") and c.contexte_rempli()
    assert [j["personnage"] for j in c.joueurs()["joueurs"]] == ["Max", ""]
    assert "silentgob" in seen["ins"] and seen["contents"] == "texte libre"


def test_contexte_deja_rempli_n_est_pas_ecrase(store, monkeypatch):
    c = store.create("Camp", "x", MJ, [MJ, P1])
    c.contexte_path.write_text("# Mon contexte à moi\n", encoding="utf-8")
    c.save_joueurs({**c.joueurs(), "joueurs": [{**c.joueurs()["joueurs"][0], "personnage": "Déjà"}]})
    monkeypatch.setattr(campagne.gemini, "generate", lambda *a, **k: REPONSE)
    res = campagne.generate_contexte(c, "x")
    assert res["fichier"] == "contexte_propose.md" and res["remplis"] == 0
    assert c.contexte_path.read_text(encoding="utf-8") == "# Mon contexte à moi\n"
    assert c.joueurs()["joueurs"][0]["personnage"] == "Déjà"


def test_reponse_mal_formee_et_json_casse():
    with pytest.raises(campagne.CampaignError):
        campagne.parse_contexte_response("n'importe quoi")
    md, mapping = campagne.parse_contexte_response("=== CONTEXTE ===\n# T\n=== JOUEURS ===\npas du json")
    assert md == "# T" and mapping == []


# --- commande -----------------------------------------------------------------------------------

class FakeInter:
    def __init__(self, perm=True, voice_members=()):
        self.user = types.SimpleNamespace(
            id=1, display_name="Couette", name="couette",
            guild_permissions=types.SimpleNamespace(manage_guild=perm),
            voice=types.SimpleNamespace(channel=types.SimpleNamespace(
                members=[types.SimpleNamespace(id=m["id"], display_name=m["name"], name=m["username"], bot=False)
                         for m in voice_members])))
        self.guild, self.guild_id, self.channel_id = types.SimpleNamespace(get_channel=lambda i: None, me=None), 7, 1
        self.out, self._done = [], False
        self.response = types.SimpleNamespace(send_message=self._send, is_done=lambda: self._done, defer=self._defer)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _send(self, content=None, **kw): self.out.append(content); self._done = True
    async def _defer(self, **kw): self._done = True


def run_cmd(bot, inter, action, **kw):
    asyncio.run(bot.tree.commands["campagne"](inter, types.SimpleNamespace(value=action), **kw))


def make_bot():
    from tests.test_commands import build
    return build()[0]


def test_commande_nouvelle_liste_choisir(store):
    bot = make_bot()
    inter = FakeInter(voice_members=[MJ, P1])
    run_cmd(bot, inter, "nouvelle", nom="Camp A")
    assert "Camp A" in inter.out[-1] and store.active().titre == "Camp A"
    assert [j["joueur"] for j in store.active().joueurs()["joueurs"]] == ["Couette", "Silent"]
    run_cmd(bot, inter, "liste")
    assert "▶ **Camp A**" in inter.out[-1]
    run_cmd(bot, inter, "choisir", nom="zzz")
    assert "introuvable" in inter.out[-1]
    bot.sessions[7] = object()
    run_cmd(bot, inter, "choisir", nom="camp")
    assert "pendant un enregistrement" in inter.out[-1]


def test_commande_permission_requise(store):
    inter = FakeInter(perm=False)
    run_cmd(make_bot(), inter, "nouvelle", nom="X")
    assert "Gérer le serveur" in inter.out[-1] and store.list() == []


def test_journal_versions_et_rollback(store):
    c = store.create("Camp", "x", MJ, [])
    with pytest.raises(campagne.CampaignError):
        campagne.rollback(c)
    with pytest.raises(campagne.CampaignError, match="vide"):
        campagne.import_journal(c, "  ")
    p1 = campagne.import_journal(c, "journal v1")
    assert p1.name == "v001_journal-initial.md" and c.resume_global().read_text(encoding="utf-8") == "journal v1\n"
    with pytest.raises(campagne.CampaignError):
        campagne.import_journal(c, "autre")
    campagne.add_version(c, "résumé v2", "2026-10-24")
    assert c.resume_global().read_text(encoding="utf-8") == "résumé v2\n"
    assert campagne.rollback(c) == ("v002_2026-10-24.md", "v001_journal-initial.md", None)
    assert c.resume_global().read_text(encoding="utf-8") == "journal v1\n"
    assert (c.path / "resume_global" / "annulees" / "v002_2026-10-24.md").exists()
    assert campagne.add_version(c, "x", "nouveau").name == "v002_nouveau.md"


def test_ajout_joueur_a_la_volee_et_association_au_contexte(store):
    c = store.create("Camp", "x", MJ, [MJ])
    c.contexte_path.write_text("# Camp\n### Max le Rouge — joué par Silent\n- x\n### Kassovitch — joué par Raph\n", encoding="utf-8")
    assert campagne.contexte_personnages(c) == ["Max le Rouge", "Kassovitch"]
    assert campagne.match_personnage(c, "kasso") == "Kassovitch" and campagne.match_personnage(c, "rouge") == "Max le Rouge"
    assert campagne.match_personnage(c, "zzz") is None
    assert campagne.add_player(c, P1, "max") == ("ajouté", [])
    assert c.joueurs()["joueurs"][0]["personnage"] == "Max le Rouge"
    status, warns = campagne.add_player(c, P2, "Max le Rouge")                 # déjà pris
    assert status == "ajouté" and "déjà joué par Silent" in warns[0]
    status, warns = campagne.add_player(c, {**P1, "username": "silent_alt"}, "Inconnu")   # mise à jour + hors contexte
    assert status == "mis à jour" and "n'est pas décrit" in warns[0]
    j = c.joueurs()["joueurs"]
    assert len(j) == 2 and j[0]["personnage"] == "Inconnu" and "silent_alt" in j[0]["pseudos"]
    with pytest.raises(campagne.CampaignError, match="MJ"):
        campagne.add_player(c, MJ)


def test_commande_joueur(store):
    c = store.create("Camp", "x", MJ, [])
    inter = FakeInter()
    member = types.SimpleNamespace(id=2, display_name="Silent", name="silentgob")
    run_cmd(make_bot(), inter, "joueur", membre=member, personnage="Max")
    assert "ajouté" in inter.out[-1] and c.joueurs()["joueurs"][0]["personnage"] == "Max"
    run_cmd(make_bot(), inter, "joueur")
    assert "paramètre `membre`" in inter.out[-1]
