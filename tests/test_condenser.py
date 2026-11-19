"""Condensation de la chronologie : blocs, regroupements proposés, niveaux (chapitre -> livre), aperçu, version, rollback."""
import pytest

from voicebot import campagne, claude_cli, condenser as cd
from tests.test_claude import camp                              # noqa: F401  (fixture)
from tests.test_analyse import Sequence as _Sequence

PREC = "Dans l'épisode précédent de Camp… Les héros ont pris le fort Thrune. Marek les a rejoints. Que cache Kintargo ?"


class Sequence(_Sequence):
    """Comme Sequence, mais le « précédemment » du condensé (second appel) est répondu à part et ne consomme rien."""
    def __call__(self, prompt, timeout_s=None):
        if "<condense>" in prompt and "TÂCHE : écrire le « Dans l'épisode précédent" in prompt:
            self.prec = getattr(self, "prec", []) + [prompt]
            return claude_cli.Reponse(PREC, 1.0, 0.0, 10, 10, "claude-sonnet-5-5")
        return super().__call__(prompt, timeout_s)

RESUME = """# Hell's Rebels

## Lieux

- Kintargo, Fort Thrune.

## Fils ouverts

- Qui a tué le garde de la Porte Noire ?

## Chronologie

Notes de départ.

### Séance du 06/10/2026
Les héros arrivent au fort Thrune. Ils rencontrent le sergent Marek, qui leur ouvre la Porte Noire. Marek parle du capitaine Lucien.

### Séance du 13/10/2026
Exploration des sous-sols du fort. Combat contre des gardes. Marek est blessé, Lucien reste introuvable.

### Séance du 20/10/2026
Fin du fort : Lucien est mort, Marek rejoint les rebelles. Le fort tombe.

### Séance du 03/11/2026
Retour à Kintargo, nouvelle mission pour Marek.
"""
BLOC = """=== TITRE ===
Le fort Thrune
=== TEXTE ===
**Résumé** : Les héros prennent le fort Thrune avec l'aide de Marek.
**Événements marquants** : Lucien est mort.
**Personnages et lieux** : Marek — sergent rallié ; Fort Thrune.
**Points ouverts** : aucun."""


def prete(camp):
    campagne.add_version(camp, RESUME, "v1")
    return camp


def test_blocs_niveaux_et_dates(camp):
    items = cd.blocs(RESUME)
    assert [b.num for b in items] == [1, 2, 3, 4] and items[0].titre == "Séance du 06/10/2026" and items[0].dates == ["06/10/2026"]
    assert items[1].texte.startswith("### Séance du 13/10/2026") and "Notes de départ" not in items[0].texte
    assert cd.niveau_de("Chapitre : x") == 1 and cd.niveau_de("Partie : x") == 1 and cd.niveau_de("Livre : x") == 2 and cd.niveau_de("Séance du 1") == 0
    with pytest.raises(campagne.CampaignError, match="Chronologie"):
        cd.blocs("# T\n\n## Lieux\n\n- a\n")
    with pytest.raises(campagne.CampaignError, match="aucun bloc"):
        cd.blocs("# T\n\n## Chronologie\n\nTexte libre.\n")


def test_plan_parse_et_validation(camp):
    items = cd.blocs(RESUME)
    plan = cd.parse_plan("B01-B03 :: Le fort Thrune :: arc terminé\n- B2–B3 :: Doublon :: chevauche\nB04-B09 :: Hors limites\nB04-B04 :: Seul\naucun")
    assert [g["de"] for g in plan] == [1, 2, 4, 4] and plan[0]["raison"] == "arc terminé"
    ok, ko = cd.valider_plan(plan, items)
    assert [g["titre"] for g in ok] == ["Le fort Thrune"] and sorted(k["motif"] for k in ko) == [
        "blocs inexistants ou moins de deux blocs", "blocs inexistants ou moins de deux blocs", "chevauche un autre regroupement"]


def test_proposer_envoie_les_blocs_et_ne_modifie_rien(camp, monkeypatch):
    prete(camp)
    ask = Sequence("B01-B03 :: Le fort Thrune :: arc conclu")
    monkeypatch.setattr(claude_cli, "ask", ask)
    out = cd.proposer(camp)
    assert [(g["de"], g["a"]) for g in out["groupes"]] == [(1, 3)] and "B01 :: Séance du 06/10/2026" in ask.prompts[0]
    assert camp.resume_global().read_text(encoding="utf-8").strip() == RESUME.strip()


def test_condenser_remplace_les_blocs_choisis_seulement(camp, monkeypatch):
    prete(camp)
    ask = Sequence(BLOC)
    monkeypatch.setattr(claude_cli, "ask", ask)
    res = cd.condenser(camp, [{"de": 1, "a": 3}])
    new = res["texte"]
    assert "### Chapitre : Le fort Thrune (du 06/10/2026 au 20/10/2026)" in new and "Séance du 13/10/2026" not in new
    assert "### Séance du 03/11/2026\nRetour à Kintargo" in new and "Notes de départ." in new and "## Lieux" in new and "## Fils ouverts" in new
    p = ask.prompts[0]
    assert "<etat_actuel>" in p and "Fils ouverts" in p and "<blocs>" in p and "Chapitre" in p and "## Chronologie" not in p.split("<etat_actuel>")[1].split("</etat_actuel>")[0]
    assert res["mots_avant"] > res["mots_apres"]
    # la version n'est écrite qu'à l'application
    assert "Chapitre" not in camp.resume_global().read_text(encoding="utf-8")
    name = cd.appliquer(camp, res)["version"]
    assert name.endswith("condensation.md") and "Chapitre : Le fort Thrune" in camp.resume_global().read_text(encoding="utf-8")
    with pytest.raises(campagne.CampaignError, match="a changé"):
        cd.appliquer(camp, res)                                                          # le résumé a bougé depuis l'aperçu
    campagne.rollback(camp)                                                              # l'annulation est celle de toute version
    assert "Séance du 13/10/2026" in camp.resume_global().read_text(encoding="utf-8")


def test_condenser_a_nouveau_chapitre_puis_livre(camp, monkeypatch):
    prete(camp)
    monkeypatch.setattr(claude_cli, "ask", Sequence(BLOC, BLOC.replace("Le fort Thrune", "Kintargo libérée")))
    first = cd.condenser(camp, [{"de": 1, "a": 3, "titre": "Le fort Thrune"}])
    cd.appliquer(camp, first)
    items = cd.blocs(camp.resume_global().read_text(encoding="utf-8"))
    assert [b.niveau for b in items] == [1, 0] and len(items) == 2
    second = cd.condenser(camp, [{"de": 1, "a": 2}])                                      # chapitre + séance -> livre
    assert second["groupes"][0]["niveau_nom"] == "Livre" and "### Livre : Kintargo libérée (du 06/10/2026 au 03/11/2026)" in second["texte"]
    assert cd.NIVEAUX == ["Séance", "Chapitre", "Livre"] and cd.niveau_cible(items, 2) == 2
    assert cd.niveau_cible([cd.Bloc(1, "Livre : x", 2, 0, 1, "")]) == 2                     # plafonné à « Livre »


SUITE = BLOC.replace("**Points ouverts** : aucun.", "**Points ouverts** : aucun.\n**Suite** : Marek mène les héros vers la Mine de Sel, leur prochain donjon ; le contremaître Orlic les attend.")


def test_suite_de_et_amorce_transmise_au_condense_suivant(camp, monkeypatch):
    assert cd.suite_de(SUITE).startswith("Marek mène les héros vers la Mine de Sel")
    assert cd.suite_de(BLOC.replace("**Points ouverts** : aucun.", "**Points ouverts** : aucun.\n**Suite** : aucune.")) == "" and cd.suite_de(BLOC) == ""
    assert "Orlic" in cd.suite_de("**Suite** : Orlic attend.\n**Autre** : x")
    prete(camp)
    ask = Sequence(SUITE, BLOC.replace("Le fort Thrune", "La Mine de Sel"))
    monkeypatch.setattr(claude_cli, "ask", ask)
    first = cd.condenser(camp, [{"de": 1, "a": 3, "titre": "Le fort Thrune"}])
    assert "\n<amorce>\n" not in ask.prompts[0] and "**Suite**" in ask.prompts[0]
    cd.appliquer(camp, first)
    # le chapitre contient sa « Suite » ; condenser l'arc suivant la reçoit comme amorce
    text = camp.resume_global().read_text(encoding="utf-8")
    assert "Mine de Sel" in text
    campagne.add_version(camp, text.replace("Retour à Kintargo, nouvelle mission pour Marek.", "Retour à Kintargo, nouvelle mission pour Marek.\n\n### Séance du 10/11/2026\nLa Mine de Sel."), "v2")
    cd.condenser(camp, [{"de": 2, "a": 3, "titre": "La Mine de Sel"}])
    amorce = ask.prompts[1].split("\n<amorce>\n")[1].split("\n</amorce>")[0]
    assert "Orlic" in amorce and not amorce.startswith("**Suite**")


def test_amorce_entre_deux_regroupements_du_meme_appel(camp, monkeypatch):
    prete(camp)
    ask = Sequence(SUITE, BLOC.replace("Le fort Thrune", "Retour"))
    monkeypatch.setattr(claude_cli, "ask", ask)
    res = cd.condenser(camp, [{"de": 1, "a": 2, "titre": "Le fort Thrune"}, {"de": 3, "a": 4, "titre": "Retour"}])
    assert "Orlic" in ask.prompts[1].split("\n<amorce>\n")[1] and len(res["groupes"]) == 2
    assert res["texte"].index("Chapitre : Le fort Thrune") < res["texte"].index("Chapitre : Retour")


def test_avertissements_noms_perdus_et_condense_trop_long(camp, monkeypatch):
    prete(camp)
    monkeypatch.setattr(claude_cli, "ask", Sequence("=== TEXTE ===\n**Résumé** : tout s'est bien passé au fort."))
    res = cd.condenser(camp, [{"de": 1, "a": 3, "titre": "Le fort"}])
    warns = " ".join(res["avertissements"])
    assert "Marek" in warns and "absents du condensé" in warns
    assert cd.noms_perdus("On voit Marek et Lucien. Puis Marek revient avec Lucien.", "Marek est là") == ["Lucien"]


def test_entree_invalide(camp, monkeypatch):
    prete(camp)
    monkeypatch.setattr(claude_cli, "ask", Sequence("pas de section"))
    with pytest.raises(campagne.CampaignError, match="Regroupement invalide"):
        cd.condenser(camp, [{"de": 3, "a": 9}])
    with pytest.raises(campagne.CampaignError, match="TEXTE"):
        cd.condenser(camp, [{"de": 1, "a": 2}])


def _seance(camp, sid, cr="Compte rendu.", prec="Dans l'épisode précédent de Camp… X."):
    d = camp.path / "seances" / sid
    d.mkdir(parents=True)
    (d / "compte_rendu.md").write_text(cr + "\n", encoding="utf-8")
    (d / "precedemment.md").write_text(prec + "\n", encoding="utf-8")
    (d / "meta.json").write_text('{"version": "x"}', encoding="utf-8")
    return d


def test_le_condense_devient_une_seance_avec_compte_rendu_et_precedemment(camp, monkeypatch):
    from voicebot import memoire
    prete(camp)
    for sid in ("2026-10-06_200000", "2026-10-13_200000", "2026-10-20_200000", "2026-11-03_200000"):
        _seance(camp, sid, cr=f"CR {sid}")
    ask = Sequence(BLOC, BLOC)
    monkeypatch.setattr(claude_cli, "ask", ask)
    res = cd.condenser(camp, [{"de": 1, "a": 3, "titre": "Le fort Thrune"}])
    g = res["groupes"][0]
    assert g["derniere_seance"] == "2026-10-20_200000" and g["precedemment"] == PREC
    assert "CR 2026-10-20_200000" in ask.prec[0] and "<condense>" in ask.prec[0] and "Dans l'épisode précédent de Camp" in ask.prec[0]
    out = cd.appliquer(camp, res)
    d = out["seances"][0]["seance"]
    assert d.name == "2026-10-20_200000_chapitre_le-fort-thrune" and d.parent == camp.path / "seances"
    assert (d / "precedemment.md").read_text(encoding="utf-8").strip() == PREC
    assert "# Chapitre : Le fort Thrune (du 06/10/2026 au 20/10/2026)" in (d / "compte_rendu.md").read_text(encoding="utf-8")
    meta = campagne._read_json(d / "meta.json", {})
    assert meta["type"] == "chapitre" and meta["version"] == out["version"] and cd.est_condense(d)
    # une séance plus récente existe (03/11) : c'est SON précédemment qui est utilisé
    assert memoire.latest_precedemment(camp).startswith("Dans l'épisode précédent de Camp… X.")
    # si le condensé couvre la dernière séance, il l'emporte
    res2 = cd.condenser(camp, [{"de": 1, "a": 2, "titre": "Tout"}])
    out2 = cd.appliquer(camp, res2)
    assert out2["seances"][0]["seance"].name.startswith("2026-11-03_200000_livre_") or out2["seances"][0]["seance"].name.startswith("2026-11-03_200000_chapitre_")
    assert memoire.latest_precedemment(camp).strip() == PREC


def test_condense_gagne_a_egalite_puis_rollback_rend_la_vraie_seance(camp, monkeypatch):
    from voicebot import memoire
    camp_ = prete(camp)
    _seance(camp_, "2026-11-03_200000", prec="Dans l'épisode précédent de Camp… REEL.")
    monkeypatch.setattr(claude_cli, "ask", Sequence(BLOC))
    out = cd.appliquer(camp_, cd.condenser(camp_, [{"de": 1, "a": 4, "titre": "Tout"}]))
    assert memoire.latest_precedemment(camp_).strip() == PREC
    campagne.rollback(camp_)
    assert not out["seances"][0]["seance"].exists() and (camp_.path / "seances" / "_annulees" / out["seances"][0]["seance"].name).exists()
    assert memoire.latest_precedemment(camp_).strip() == "Dans l'épisode précédent de Camp… REEL."


def test_condense_ne_compte_pas_comme_seance_en_attente_et_s_affiche_dans_les_choix(camp, monkeypatch, tmp_path):
    from voicebot import memoire, pack
    prete(camp)
    monkeypatch.setattr(claude_cli, "ask", Sequence(BLOC))
    d = cd.appliquer(camp, cd.condenser(camp, [{"de": 1, "a": 3, "titre": "Fort"}]))["seances"][0]["seance"]
    assert pack.pending_session(camp) is None
    assert any(d.name in i and "Chapitre" in lbl for i, lbl in memoire.session_choices(camp, tmp_path / "vide"))
    assert cd.dernier_sid(camp, [cd.Bloc(1, "Séance du 06/10/2026", 0, 0, 1, "", dates=["06/10/2026"])]) == "2026-10-06_999999"


def test_condense_sans_date_ne_passe_pas_pour_la_derniere_seance(camp):
    B = lambda n, dates=(): cd.Bloc(n, f"Bloc {n}", 0, 0, 1, "", dates=list(dates))                    # noqa: E731
    _seance(camp, "2026-10-06_200000")
    _seance(camp, "2026-11-03_200000")
    tous = [B(1), B(2), B(3, ["06/10/2026"]), B(4), B(5)]
    assert cd.dernier_sid(camp, tous[:2], tous) == cd.AVANT_TOUT                   # rien de daté avant : rangé avant tout
    assert cd.dernier_sid(camp, tous[3:4], tous) == "2026-10-06_200000"            # après le dernier bloc daté qui précède
    assert cd.dernier_sid(camp, tous[3:], tous) == "2026-11-03_200000"             # va jusqu'au bout : couvre la dernière séance
    assert cd.AVANT_TOUT < "2026-10-06_200000"


def test_compteur_de_seances_sans_condenses_ni_dossiers_non_integres(camp, monkeypatch):
    prete(camp)
    _seance(camp, "2026-10-06_200000")
    (camp.path / "seances" / "2026-10-13_200000" / "analyse").mkdir(parents=True)          # analyse en cours : pas intégrée
    monkeypatch.setattr(claude_cli, "ask", Sequence(BLOC))
    cd.appliquer(camp, cd.condenser(camp, [{"de": 1, "a": 3, "titre": "Fort"}]))
    assert camp.nb_seances() == 1 and camp.nb_condenses() == 1 and camp.seances_texte() == "1 séance(s), 1 condensé(s)"
