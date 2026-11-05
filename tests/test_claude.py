"""Résumé automatique avec Claude (abonnement) : fusion « delta » du résumé global, appel du programme, /campagne resumer."""
import asyncio
import json
import os
import types

import pytest

from voicebot import campagne, claude_cli, fusion, memoire, pack
from voicebot.config import SET
from tests.test_campagne import FakeInter, MJ, P1, make_bot
from tests.test_memoire import Att, ROWS

SID = "2026-10-30_200000"
ETAT = """# Résumé global : Camp

## Changements depuis la version précédente
- ancien changement

## Chronologie
### Séance du 24/10/2026
- Le groupe arrive à Kintargo (butin : 3 potions, 120 po).
- Max subit 43 dégâts.

## Personnages joueurs : situation actuelle
- Max : blessé, 12 PV sur 40 (détail important à ne pas perdre, long texte pour dépasser le seuil de signalement). """ + "x" * 400 + """

## Fils ouverts
- Qui est le chef ?

## Réserves
- Nom du PNJ à confirmer
"""

DELTA = """=== FIN LUE === 00:00:08 | « J'ouvre la porte. »
=== NOTES ===
- [00:00:04] Max ouvre la porte ●
=== COMPTE RENDU ===
# Compte rendu du 30/10
Max ouvre la porte.
=== CHANGEMENTS ===
- Max a ouvert la porte
- Le chef est identifié
=== CHRONOLOGIE ===
### Séance du 30/10/2026
- Max ouvre la porte et découvre une salle.
=== SECTIONS ===
--- SECTION: Fils ouverts ---
- Que cache la salle ?
--- SECTION: Réserves ---
- Nom du PNJ à confirmer
- Contradiction : le chef serait déjà mort
=== PRECEDEMMENT ===
Dans l'épisode précédent de Camp… Max a ouvert la porte."""


@pytest.fixture
def camp(tmp_path, monkeypatch, cfg_file):
    monkeypatch.setitem(SET.values, "campagnes.dossier", str(tmp_path / "camp"))
    monkeypatch.setitem(SET.values, "sessions.dossier", str(tmp_path / "s"))
    c = campagne.CampaignStore(tmp_path / "camp").create("Camp", "Pathfinder", MJ, [MJ, P1])
    campagne.add_version(c, ETAT, "etat initial")
    return c


# --- fusion ----------------------------------------------------------------------------------------

def test_fusion_ne_reecrit_jamais_les_anciennes_rubriques():
    new, warns = fusion.merge(ETAT, "### Séance du 30/10/2026\n- Max ouvre la porte.",
                              [("Fils ouverts", "- Que cache la salle ?")], "- Max a ouvert la porte")
    assert "- Le groupe arrive à Kintargo (butin : 3 potions, 120 po)." in new and "- Max subit 43 dégâts." in new      # ancien texte intact
    assert new.index("Séance du 24/10") < new.index("Séance du 30/10") < new.index("## Personnages joueurs")        # ajout en fin de chronologie
    assert "Que cache la salle" in new and "Qui est le chef" not in new                                           # rubrique remplacée
    assert "detail important" not in new or "détail important" in new                                              # rubrique non citée : intacte
    assert "ancien changement" not in new and "- Max a ouvert la porte" in new and new.count("## Changements") == 1
    assert warns == []


def test_fusion_signale_une_rubrique_qui_maigrit_ou_qui_manque():
    _, warns = fusion.merge(ETAT, "- x", [("Personnages joueurs", "- Max va bien"), ("Bestiaire", "- gobelin")])
    assert any("raccourcie" in w and "Personnages joueurs" in w for w in warns)
    assert any("Bestiaire" in w and "ajoutée" in w for w in warns)


def test_fusion_resume_sans_chronologie_ni_titres():
    new, warns = fusion.merge("Notes libres des joueurs.\nLigne 2.", "- Nouveau", [], "- Premier résumé")
    assert new.startswith("## Changements") or "Notes libres des joueurs." in new
    assert "Notes libres des joueurs.\nLigne 2." in new and "## Chronologie" in new and "- Nouveau" in new
    assert any("Chronologie" in w for w in warns)


def test_fusion_debut_de_campagne():
    new, _ = fusion.merge("", "### Séance du 24/10/2026\n- Début", [("Lieux", "- Kintargo"), ("Fils ouverts", "- ?")],
                          "- Création", document_title="Résumé global : Camp")
    assert new.startswith("# Résumé global : Camp") and fusion.titles(new)[:2] == ["Résumé global : Camp", "Changements depuis la version précédente"]
    assert "## Chronologie" in new and "## Lieux" in new and "- Kintargo" in new


def test_fusion_titres_non_markdown_et_sous_rubriques():
    old = "**Chronologie**\n- A\n\n---------- FILS OUVERTS ----------\n- B\n"
    new, _ = fusion.merge(old, "- C", [("Fils ouverts", "- D")])
    assert new.index("- A") < new.index("- C") < new.index("FILS OUVERTS") and "- B" not in new and "- D" in new
    nested = "## Lieux\n### Kintargo\n- ville\n### Ville B\n- autre\n## Fils ouverts\n- ?\n"
    new2, _ = fusion.merge(nested, "- c", [("Lieux", "- tout refait")])
    assert "tout refait" in new2 and "Kintargo" not in new2 and "## Fils ouverts\n- ?" in new2


def test_parse_des_sections_du_modele():
    got = fusion.parse_sections("--- SECTION: Fils ouverts ---\n- a\n\n**--- SECTION : Réserves ---**\n- b\n--- SECTION: Vide ---\n")
    assert got == [("Fils ouverts", "- a"), ("Réserves", "- b")]


# --- réponse et intégration ------------------------------------------------------------------------

def test_parse_reponse_delta_et_ancien_format():
    d = pack.parse_reponse(DELTA)
    assert d["resume_global"] is None and d["delta"]["chronologie"].startswith("### Séance du 30/10/2026")
    assert [t for t, _ in d["delta"]["sections"]] == ["Fils ouverts", "Réserves"] and "identifié" in d["delta"]["changements"]
    legacy = pack.parse_reponse("=== NOTES ===\nn\n=== COMPTE RENDU ===\ncr\n=== RESUME GLOBAL ===\n# Tout\n=== PRECEDEMMENT ===\np")
    assert legacy["delta"] is None and legacy["resume_global"] == "# Tout"
    with pytest.raises(campagne.CampaignError, match="CHRONOLOGIE"):
        pack.parse_reponse("=== COMPTE RENDU ===\ncr\n=== PRECEDEMMENT ===\np")


def test_integration_delta_ajoute_une_version_et_se_rollback(camp):
    before = camp.resume_global().read_text(encoding="utf-8")
    pack.build_packs(camp, ROWS[:3], SID, 0)
    out = pack.integrate(camp, DELTA, SID, source="claude", extra={"modele": "m"})
    new = camp.resume_global().read_text(encoding="utf-8")
    assert "Max subit 43 dégâts." in new and "Max ouvre la porte et découvre" in new and "Que cache la salle" in new
    assert out["avertissement"] is None
    meta = json.loads((camp.path / "seances" / SID / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "claude" and meta["modele"] == "m" and meta["version"] == out["version"]
    assert len(campagne.versions(camp)) == 2
    assert campagne.rollback(camp)[2] == SID and camp.resume_global().read_text(encoding="utf-8") == before


def test_precedemment_trop_long_est_signale(camp):
    long_answer = DELTA.replace("Max a ouvert la porte.", "mot " * 300)
    pack.build_packs(camp, ROWS[:3], SID, 0)
    assert "mots" in pack.integrate(camp, long_answer, SID)["avertissement"]


def test_pack_allege_et_consigne_resserree(camp):
    pack.build_packs(camp, ROWS[:4], SID, 0)
    text = (camp.path / "seances" / SID / "pack" / f"pack_{SID}.md").read_text(encoding="utf-8")
    assert "[00:00:04] Max le Rouge (Silent): merci J'ouvre la porte. doucement." in text or "merci J'ouvre la porte. doucement." in text
    assert text.count("Silent): ") == 1                                                       # une seule ligne pour les 3 répliques
    assert "UNIQUEMENT la séance du 30/10/2026" in text and "Ne recopie JAMAIS l'état actuel" in text
    assert "Rubriques de l'état actuel : « Résumé global : Camp »" in text and "« Chronologie »" in text
    SET.values["campagnes.pack_fusion_s"] = 0
    pack.build_packs(camp, ROWS[:4], SID, 0)
    assert (camp.path / "seances" / SID / "pack" / f"pack_{SID}.md").read_text(encoding="utf-8").count("Silent): ") == 3


# --- appel de claude -------------------------------------------------------------------------------

def test_appel_de_claude_options_environnement_et_dossier(faux_claude, monkeypatch):
    faux_claude.answer = "ok"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    rep = claude_cli.ask("Bonjour é")
    call = faux_claude.calls[0]
    argv = call["argv"]
    assert argv[:4] == ["/usr/bin/claude", "-p", "--output-format", "json"]
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--tools") + 1] == "" and "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert "--safe-mode" in argv and "--disable-slash-commands" in argv and "Bonjour" not in " ".join(argv)    # texte par stdin, pas en argument
    assert call["input"] == "Bonjour é" and "ANTHROPIC_API_KEY" not in call["env"] and "ANTHROPIC_AUTH_TOKEN" not in call["env"]
    assert call["env"]["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "5m" and os.environ["ANTHROPIC_API_KEY"] == "sk-secret"
    assert rep.texte == "ok" and rep.tokens_entree == 190010 and rep.tokens_sortie == 7700 and rep.duree_s == 54 and rep.cout_usd == 0.13
    SET.values["claude.options"] = ""
    claude_cli.ask("x")
    assert "--safe-mode" not in faux_claude.calls[1]["argv"]


@pytest.mark.parametrize("extra,stderr,code,needle", [
    ({"is_error": True, "result": "Failed to authenticate: OAuth session expired"}, "", 1, "/login"),
    ({}, "error: unknown option '--safe-mode'", 1, "options"),
    ({"is_error": True, "result": "Claude usage limit reached"}, "", 1, "Limite d'usage"),
    ({"result": ""}, "", 0, "sans texte"),
])
def test_erreurs_de_claude_expliquees(faux_claude, extra, stderr, code, needle):
    faux_claude.result_extra, faux_claude.stderr, faux_claude.returncode = extra, stderr, code
    faux_claude.answer = "x" if "result" not in extra else extra["result"]
    with pytest.raises(claude_cli.ClaudeError, match=needle):
        claude_cli.ask("x")


def test_claude_introuvable(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    assert "introuvable" in claude_cli.reason_unavailable()
    with pytest.raises(claude_cli.ClaudeError, match="introuvable"):
        claude_cli.ask("x")


# --- commande /campagne resumer --------------------------------------------------------------------

def resumer(bot, inter, **kw):
    asyncio.run(bot.tree.commands["campagne"](inter, types.SimpleNamespace(value="resumer"), **kw))


TRANSCRIPT = ("[00:00:01.000 → 00:00:04.000] couette : Vous arrivez devant la porte.\n"
              "[00:00:05.000 → 00:00:08.000] silentgob : J'ouvre la porte.")


def test_resumer_avec_claude_integre_tout_automatiquement(camp, faux_claude):
    faux_claude.answer = DELTA
    bot, inter = make_bot(), FakeInter()
    inter.channel_id = 1
    resumer(bot, inter, session=SID, fichier=Att(TRANSCRIPT))
    text = "\n".join(str(o) for o in inter.out)
    assert "intégrée" in text and "rollback" in text and "sonnet" in text and "190 010 tokens lus" in text
    assert memoire.latest_precedemment(camp).startswith("Dans l'épisode précédent de Camp")
    assert "Max ouvre la porte et découvre" in camp.resume_global().read_text(encoding="utf-8")
    assert "Séance du 24/10/2026" in camp.resume_global().read_text(encoding="utf-8")
    prompt = faux_claude.calls[0]["input"]
    assert "Kintargo" in prompt and "<transcription>" in prompt and "J'ouvre la porte." in prompt        # l'état actuel part avec le pack
    inter2 = FakeInter()
    resumer(bot, inter2, session=SID, fichier=Att(TRANSCRIPT))
    assert "déjà intégrée" in inter2.out[-1] and len(faux_claude.calls) == 1                              # pas de second appel
    inter3 = FakeInter()
    asyncio.run(bot.tree.commands["campagne"](inter3, types.SimpleNamespace(value="rollback")))
    assert "annulée" in inter3.out[-1] and "Séance" in inter3.out[-1] and memoire.latest_precedemment(camp) is None


def test_resumer_reponse_inutilisable_ne_touche_a_rien(camp, faux_claude):
    faux_claude.answer = "Désolé, je ne peux pas."
    before = camp.resume_global().read_text(encoding="utf-8")
    bot, inter = make_bot(), FakeInter()
    resumer(bot, inter, session=SID, fichier=Att(TRANSCRIPT))
    assert "❌" in inter.out[-1] and "incomplète" in inter.out[-1]
    assert camp.resume_global().read_text(encoding="utf-8") == before and len(campagne.versions(camp)) == 1
    assert (camp.path / "seances" / SID / "reponse_echec.md").read_text(encoding="utf-8").startswith("Désolé")
    assert not (camp.path / "seances" / SID / "meta.json").exists()


def test_resumer_sans_claude_ou_pendant_un_enregistrement(camp, faux_claude, monkeypatch):
    bot, inter = make_bot(), FakeInter()
    bot.sessions[7] = object()
    resumer(bot, inter, session=SID, fichier=Att(TRANSCRIPT))
    assert "pendant un enregistrement" in inter.out[-1] and faux_claude.calls == []
    bot.sessions.clear()
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    inter2 = FakeInter()
    resumer(bot, inter2, session=SID, fichier=Att(TRANSCRIPT))
    assert "Claude indisponible" in inter2.out[-1] and "gemini" in inter2.out[-1]


def test_resumer_erreur_de_connexion_claude(camp, faux_claude):
    faux_claude.result_extra, faux_claude.returncode = {"is_error": True, "result": "OAuth session expired"}, 1
    bot, inter = make_bot(), FakeInter()
    resumer(bot, inter, session=SID, fichier=Att(TRANSCRIPT))
    assert "❌" in inter.out[-1] and "/login" in inter.out[-1] and len(campagne.versions(camp)) == 1
