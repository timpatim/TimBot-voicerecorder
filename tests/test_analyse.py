"""Pipeline en trois appels (nettoyage, analyse + questions, version finale), glossaire appris et reprise après échec."""
import pytest

from voicebot import analyse, campagne, claude_cli, glossaire, memoire
from voicebot.config import SET
from tests.test_claude import DELTA, camp, ETAT                # noqa: F401  (fixture)
from tests.test_memoire import ROWS

SID = "2026-10-30_200000"
CLEAN = """=== CORRECTIONS ===
- L0001 :: devant la porte => devant la Porte Noire :: nom du lieu :: ●
L0002 :: introuvable => x :: raison :: ●
L0002 :: doucement => ∅ :: bruit :: ◐
L0009 :: a => b :: ligne inexistante :: ●
=== GLOSSAIRE ===
- Porte Noire : porte noir, Port Noire
- Fantôme : fantôme inconnu
=== BILAN ===
Qualité moyenne. Passage peu fiable vers 00:00:05."""
QUESTIONS = """
=== QUESTIONS ===
--- QUESTION 1 ---
Question: Le garde de la porte est-il le même que celui de la première scène ?
Pourquoi: change le compte rendu
Repères: [00:00:01] [00:00:05]
Choix: Oui | Non | Un sosie
--- QUESTION 2 ---
Question: Qui a ouvert ?
Pourquoi: attribution
Repères: [00:00:05]
Choix: Max | Le garde"""


class Sequence:
    """Remplace claude_cli.ask : réponses successives, prompts enregistrés."""
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt, timeout_s=None):
        self.prompts.append(prompt)
        return claude_cli.Reponse(self.answers.pop(0), 10.0, 0.05, 1000, 500, "claude-sonnet-5-5")


def rows():
    return [dict(r) for r in ROWS[:4]]


def final_answer():
    return DELTA.split("=== NOTES ===")[0].replace("=== FIN LUE === 00:00:08 | « J'ouvre la porte. »\n", "") + \
        "=== COMPTE RENDU ===" + DELTA.split("=== COMPTE RENDU ===")[1].replace("Max ouvre la porte.", "Max (le garde) ouvre la porte.")


def test_parse_corrections_formats():
    got = analyse.parse_corrections("- L0001 :: a b => c :: raison :: ●\nL2 :: x → y :: ○\nn'importe quoi\nL3 :: z => ∅")
    assert [(c["ligne"], c["ancien"], c["nouveau"], c["confiance"]) for c in got] == [
        (1, "a b", "c", "●"), (2, "x", "y", "○"), (3, "z", "∅", "◐")]


def test_apply_corrections_valide_et_rejette():
    base = [[0, 1, "MJ", "devant la porte noir"], [2, 3, "Max", "doucement"], [4, 5, "Max", "bonjour"]]
    cs = [{"ligne": 1, "ancien": "porte noir", "nouveau": "Porte Noire", "raison": "", "confiance": "●"},
          {"ligne": 1, "ancien": "inexistant", "nouveau": "x", "raison": "", "confiance": "●"},
          {"ligne": 2, "ancien": "doucement", "nouveau": "∅", "raison": "", "confiance": "◐"},
          {"ligne": 3, "ancien": "bonjour", "nouveau": "x " * 20, "raison": "", "confiance": "●"},
          {"ligne": 3, "ancien": "BONJOUR", "nouveau": "salut", "raison": "", "confiance": "○"},
          {"ligne": 7, "ancien": "a", "nouveau": "b", "raison": "", "confiance": "●"}]
    rows_, ok, ko, warns = analyse.apply_corrections(base, cs)
    assert [r[3] for r in rows_] == ["devant la Porte Noire", "bonjour"]              # la ligne vidée disparaît
    assert len(ok) == 2 and [c["motif"] for c in ko] == ["texte à remplacer introuvable dans la ligne", "ajout démesuré",
                                                         "confiance trop faible", "ligne inexistante"]
    assert base[0][3] == "devant la porte noir" and warns and "%" in warns[0]            # l'entrée n'est pas modifiée ; trop de lignes touchées


def test_casse_ignoree_en_second_choix():
    rows_, ok, _ko, _w = analyse.apply_corrections([[0, 1, "MJ", "Garm arrive"]], [{"ligne": 1, "ancien": "garm", "nouveau": "Garmoth",
                                                                                      "raison": "", "confiance": "●"}])
    assert rows_[0][3] == "Garmoth arrive" and ok


def test_parse_questions():
    qs = analyse.parse_questions(QUESTIONS.split("=== QUESTIONS ===")[1], 6)
    assert [q.id for q in qs] == ["q1", "q2"] and qs[0].reperes == [1, 5] and qs[0].choix == ["Oui", "Non", "Un sosie"]
    assert len(analyse.parse_questions(QUESTIONS, 1)) == 1


def test_pipeline_complet_trois_appels(camp, monkeypatch):
    ask = Sequence(CLEAN, DELTA + QUESTIONS, final_answer())
    monkeypatch.setattr(claude_cli, "ask", ask)
    monkeypatch.setattr(claude_cli, "reason_unavailable", lambda: None)
    steps = []
    a = analyse.Analyse(camp, rows(), SID, nettoyage=True, questions=True, progress=steps.append)
    info = a.nettoyer()
    assert len(info["appliquees"]) == 2 and len(info["rejetees"]) == 2 and "Qualité moyenne" in info["bilan"]
    assert "L0001 [00:00:00]" in ask.prompts[0] and "<transcription>" in ask.prompts[0]
    corrected = (camp.path / "seances" / SID / "analyse" / "transcript_corrige.txt").read_text(encoding="utf-8")
    assert "devant la Porte Noire" in corrected and "doucement" not in corrected
    qs = a.analyser()
    assert len(qs) == 2 and "Porte Noire" in ask.prompts[1] and "=== QUESTIONS ===" in ask.prompts[1] and "6 questions" in ask.prompts[1]
    out = a.finaliser({"q1": {"reponse": "Oui", "par": "Couette"}, "q2": {"reponse": analyse.JE_NE_SAIS_PAS, "par": "Couette"}})
    p3 = ask.prompts[2]
    assert "<brouillon>" in p3 and "Oui (par Couette)" in p3 and "non précisé" in p3 and "<extraits>" in p3 and "Porte Noire" in p3
    assert "=== QUESTIONS ===" not in p3.split("<brouillon>")[1].split("</brouillon>")[0]
    assert "Max (le garde) ouvre" in (camp.path / "seances" / SID / "compte_rendu.md").read_text(encoding="utf-8")
    assert out["meta"]["appels"] == 3 and out["meta"]["questions"] == 2 and out["version"]
    # glossaire : variante réellement présente seulement + décision des joueurs
    g = (camp.path / "glossaire.md").read_text(encoding="utf-8")
    assert "Fantôme" not in g and "Oui" in g and "Couette" in g
    assert "Glossaire appris" in memoire.contexte_text(camp)
    # rollback : le glossaire revient à son état d'avant
    campagne.rollback(camp)
    assert "Oui" not in (camp.path / "glossaire.md").read_text(encoding="utf-8")


def test_glossaire_filtre_les_variantes_absentes(camp):
    props = glossaire.parse_termes("Porte Noire : porte noir, Port Noire\nFantôme : fantôme inconnu")
    assert glossaire.filtrer_termes(props, "[00:00:01] MJ: devant la porte noir") == [("Porte Noire", ["porte noir"])]


def test_sans_question_deux_appels_et_reprise_apres_echec(camp, monkeypatch):
    monkeypatch.setattr(claude_cli, "reason_unavailable", lambda: None)
    ask = Sequence(CLEAN, "Je ne peux pas.")
    monkeypatch.setattr(claude_cli, "ask", ask)
    with pytest.raises(campagne.CampaignError):
        analyse.run_sans_question(camp, rows(), SID, nettoyage=True, questions=False)
    assert len(ask.prompts) == 2
    ask2 = Sequence(DELTA)                                      # relance : le nettoyage est repris du cache, seule l'analyse est refaite
    monkeypatch.setattr(claude_cli, "ask", ask2)
    # la réponse inutilisable est mémorisée par empreinte : on la retire pour simuler le rejeu complet
    (camp.path / "seances" / SID / "analyse" / "2_analyse.sha").unlink()
    out = analyse.run_sans_question(camp, rows(), SID, nettoyage=True, questions=False)
    assert len(ask2.prompts) == 1 and out["meta"]["appels"] == 1 and out["meta"]["appels_repris"] == 1
    assert "intégrée" not in out and out["version"]


def test_modes_sans_nettoyage(camp, monkeypatch):
    ask = Sequence(DELTA)
    monkeypatch.setattr(claude_cli, "ask", ask)
    out = analyse.run_sans_question(camp, rows(), SID, nettoyage=False, questions=False)
    assert len(ask.prompts) == 1 and "=== QUESTIONS ===" not in ask.prompts[0] and out["meta"]["appels"] == 1


def test_erreur_claude_devient_analyse_error(camp, monkeypatch):
    def boom(prompt, timeout_s=None):
        raise claude_cli.ClaudeError("hors ligne")
    monkeypatch.setattr(claude_cli, "ask", boom)
    with pytest.raises(analyse.AnalyseError, match="hors ligne"):
        analyse.Analyse(camp, rows(), SID, nettoyage=True, questions=True).nettoyer()


# --- commande /campagne resumer (pipeline complet) et questions ---------------------------------

import asyncio
import types

from voicebot import questions_ui
from tests.test_campagne import FakeInter, make_bot
from tests.test_memoire import Att


class Msg:
    def __init__(self, content, log):
        self.content, self.log = content, log

    async def edit(self, content=None, **kw):
        self.content = content
        self.log.append(("edit", content))


class Salon:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content if content is not None else kw.get("embed"))
        return Msg(content, self.sent)


TRANSCRIPT = ("[00:00:01.000 → 00:00:04.000] couette : Vous arrivez devant la porte.\n"
              "[00:00:05.000 → 00:00:08.000] silentgob : J'ouvre la porte.")


def lance(camp, monkeypatch, answers, reponses, *, nettoyage=True, questions=True):
    ask = Sequence(*answers)
    monkeypatch.setattr(claude_cli, "ask", ask)
    monkeypatch.setattr(claude_cli, "reason_unavailable", lambda: None)
    monkeypatch.setitem(SET.values, "campagnes.moteur_resume", "claude")
    monkeypatch.setitem(SET.values, "claude.nettoyage", nettoyage)
    monkeypatch.setitem(SET.values, "claude.questions", questions)
    asked = []

    async def poser(channel, qs, delai_s, deja=None):
        asked.append((qs, delai_s, deja))
        return reponses
    monkeypatch.setattr(questions_ui, "poser", poser)
    salon, inter = Salon(), FakeInter()
    inter.channel = salon
    asyncio.run(make_bot().tree.commands["campagne"](inter, types.SimpleNamespace(value="resumer"), session=SID, fichier=Att(TRANSCRIPT)))
    return ask, asked, salon, inter


def test_commande_pipeline_complet(camp, monkeypatch):
    ask, asked, salon, inter = lance(camp, monkeypatch, [CLEAN.replace("L0001 :: devant la porte", "L0001 :: devant la porte"), DELTA + QUESTIONS, final_answer()],
                                     {"q1": {"reponse": "Oui", "par": "Couette"}})
    text = "\n".join(str(x) for x in salon.sent)
    assert len(ask.prompts) == 3 and asked[0][1] == 30 * 60 and len(asked[0][0]) == 2
    assert "Bilan de qualité" in text and "intégrée" in text and "3 appels" in text and "glossaire" in text and "rollback" in text
    assert "Analyse lancée" in inter.out[0] and "Max (le garde)" in (camp.path / "seances" / SID / "compte_rendu.md").read_text(encoding="utf-8")


def test_commande_sans_question_ne_demande_rien(camp, monkeypatch):
    ask, asked, salon, _ = lance(camp, monkeypatch, [CLEAN, DELTA], {})
    assert len(ask.prompts) == 2 and not asked and "2 appels" in "\n".join(str(x) for x in salon.sent)


def test_commande_echec_garde_les_appels_reussis(camp, monkeypatch):
    ask, _asked, salon, _ = lance(camp, monkeypatch, [CLEAN, "Je ne sais pas faire."], {})
    text = "\n".join(str(x) for x in salon.sent)
    assert "Analyse interrompue" in text and "Rien n'a été intégré" in text and not (camp.path / "seances" / SID / "meta.json").exists()
    assert (camp.path / "seances" / SID / "analyse" / "1_nettoyage.md").exists()


def test_collecteur_et_choix():
    async def main():
        qs = analyse.parse_questions(QUESTIONS.split("=== QUESTIONS ===")[1], 6)
        col = questions_ui.Collecteur(qs, {"q1": {"reponse": "Oui", "par": "A"}, "zzz": {"reponse": "x", "par": "A"}})
        assert list(col.reponses) == ["q1"] and not col.fini.is_set() and [q.id for q in col.manquantes()] == ["q2"]
        assert not await col.attendre(0.01)                                  # délai écoulé : pas bloquant
        assert not col.repondre("q2", "  ", "B") and not col.repondre("nope", "x", "B")
        assert col.repondre("q2", "Max", "B")
        assert not col.fini.is_set()                                         # tout est répondu mais rien n'est validé
        assert col.repondre("q2", "Le garde", "B") and col.reponses["q2"]["reponse"] == "Le garde"   # on peut changer d'avis
        col.valider()
        assert col.fini.is_set() and await col.attendre(1)
        assert not col.repondre("q2", "Max", "B") and col.reponses["q2"]["reponse"] == "Le garde"   # figé après validation
        assert questions_ui.reponse_du_choix(qs[0], "1") == "Non" and questions_ui.reponse_du_choix(qs[0], questions_ui.AUTRE) is None
        assert questions_ui.reponse_du_choix(qs[0], questions_ui.NSP) == analyse.JE_NE_SAIS_PAS
        assert "Question 1/2" in questions_ui.texte_question(qs[0], 1, 2) and "`00:00:01`" in questions_ui.texte_question(qs[0], 1, 2)
    asyncio.run(main())


def test_decisions_etiquetees_exclues_de_leur_seance_et_purgees(camp, monkeypatch):
    monkeypatch.setattr(claude_cli, "ask", Sequence(CLEAN, DELTA + QUESTIONS, final_answer()))
    monkeypatch.setattr(claude_cli, "reason_unavailable", lambda: None)
    a = analyse.Analyse(camp, rows(), SID, nettoyage=True, questions=True)
    a.nettoyer()
    a.analyser()
    a.finaliser({"q1": {"reponse": "Pas Lucien", "par": "Couette"}})
    g = (camp.path / "glossaire.md").read_text(encoding="utf-8")
    assert glossaire.etiquette(SID) in g and "Pas Lucien" in g
    assert "Pas Lucien" in memoire.contexte_text(camp)                         # visible des autres séances...
    assert "Pas Lucien" not in memoire.contexte_text(camp, SID)                # ...jamais de sa propre séance
    # le relevé de la séance est perdu (ou la ligne a été retouchée) : le rollback retire quand même ses décisions
    for f in (camp.path / "seances" / SID / "analyse").glob("glossaire_ajouts.txt"):
        f.unlink()
    campagne.rollback(camp)
    assert "Pas Lucien" not in (camp.path / "glossaire.md").read_text(encoding="utf-8")
    assert glossaire.purger(camp.path, SID) == 0


# --- /glossaire corriger -------------------------------------------------------------------------

from voicebot import glossaire_corr as gc

REPONSE_CORR = """=== OPERATIONS ===
- G02 :: SUPPRIMER :: démentie par la précision
G03 :: REMPLACER :: 03/11/2026 · Couette · Qui est le chef du fort ? → Thrune :: corrigé
G09 :: SUPPRIMER :: n'existe pas
G01 :: REMPLACER :: Porte Noire : porte noir :: identique
AJOUTER :: Thrune : Trune :: nouveau terme
=== RESUME ===
Le chef du fort est Thrune, pas Lucien."""


def glossaire_de_test(camp):
    glossaire.ajouter(camp.path, [("Porte Noire", ["porte noir"])],
                      ["03/11/2026 · Couette · Le chef est Lucien ? → Oui " + glossaire.etiquette(SID),
                       "03/11/2026 · Couette · Qui est le chef du fort ? → Lucien " + glossaire.etiquette(SID)])


def test_parse_et_validation_des_operations(camp):
    glossaire_de_test(camp)
    items = gc.entrees(camp.path)
    assert [n for n, _ in items] == [1, 2, 3] and items[0][1].startswith("Porte Noire")
    ops, resume = gc.parse_operations(REPONSE_CORR)
    assert [o["op"] for o in ops] == ["SUPPRIMER", "REMPLACER", "SUPPRIMER", "REMPLACER", "AJOUTER"] and "Thrune" in resume
    ok, ko, _w = gc.valider(ops, items)
    assert [o["num"] for o in ok] == [2, 3, None] and sorted(k["motif"] for k in ko) == ["aucun changement", "entrée inexistante"]


def test_corriger_apercu_application_et_annulation(camp, monkeypatch):
    glossaire_de_test(camp)
    ask = Sequence(REPONSE_CORR)
    monkeypatch.setattr(claude_cli, "ask", ask)
    before = (camp.path / "glossaire.md").read_text(encoding="utf-8")
    prop = gc.proposer(camp, "Le chef du fort n'est pas Lucien, c'est Thrune", "Couette")
    assert "<precision auteur=\"Couette\">" in ask.prompts[0] and "G02 " in ask.prompts[0] and "Porte Noire" in ask.prompts[0]
    assert (camp.path / "glossaire.md").read_text(encoding="utf-8") == before          # l'aperçu ne modifie rien
    assert "Précision notée" in gc.apercu(prop["ok"], prop["ko"], prop["precision_ligne"])
    done = gc.appliquer(camp.path, prop)
    now = (camp.path / "glossaire.md").read_text(encoding="utf-8")
    assert done["supprimees"] == 1 and done["remplacees"] == 1 and done["ajoutees"] == 2
    assert "Le chef est Lucien" not in now and "→ Thrune" in now and glossaire.etiquette(SID) in now     # l'étiquette de séance est conservée
    assert "- Thrune : Trune" in now
    assert "Précision (correction) : Le chef du fort n'est pas Lucien, c'est Thrune" in now
    assert (camp.path / "glossaire_versions" / done["version"]).read_text(encoding="utf-8") == before
    assert gc.annuler(camp.path) == done["version"] and (camp.path / "glossaire.md").read_text(encoding="utf-8") == before
    with pytest.raises(campagne.CampaignError, match="Rien à annuler"):
        gc.annuler(camp.path)


def test_corriger_refuse_si_le_glossaire_a_change_et_precision_trop_courte(camp, monkeypatch):
    glossaire_de_test(camp)
    monkeypatch.setattr(claude_cli, "ask", Sequence(REPONSE_CORR))
    prop = gc.proposer(camp, "Le chef du fort n'est pas Lucien", "Couette")
    glossaire.purger(camp.path, SID)                                                     # quelqu'un modifie le glossaire entre-temps
    with pytest.raises(campagne.CampaignError, match="a changé"):
        gc.appliquer(camp.path, prop)
    with pytest.raises(campagne.CampaignError, match="une phrase"):
        gc.proposer(camp, "non", "Couette")


def test_corriger_rien_a_changer(camp, monkeypatch):
    glossaire_de_test(camp)
    monkeypatch.setattr(claude_cli, "ask", Sequence("=== OPERATIONS ===\naucune\n=== RESUME ===\nRien ne change."))
    prop = gc.proposer(camp, "Le ciel est bleu, vraiment.", "Couette")
    assert prop["ok"] == [] and prop["resume"] == "Rien ne change."
    done = gc.appliquer(camp.path, prop)                                                 # seule la précision est notée
    assert done["supprimees"] == done["remplacees"] == 0 and "Le ciel est bleu" in (camp.path / "glossaire.md").read_text(encoding="utf-8")
