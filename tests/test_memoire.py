"""Mémoire de campagne : nettoyage, cascade de résumés (Gemini simulé), pipeline, rollback, /record et /campagne resumer."""
import asyncio
import json
import types

import pytest

from voicebot import campagne, commands, memoire
from voicebot.config import SET
from voicebot.pipeline import PIPELINE_STEPS, StatusBoard, run_pipeline
from tests.test_campagne import FakeInter, MJ, P1, make_bot


class FakeGemini:
    def __init__(self): self.calls = []

    def __call__(self, instruction, contents, **kw):
        self.calls.append(contents)
        if "extraire les événements" in contents:
            return "- [00:00:05] Max entre dans la ville ●"
        if "écrire le compte rendu" in contents:
            return "# Compte rendu\nMax est entré."
        if "mettre à jour le résumé global" in contents:
            return "# État\nMax est en ville."
        if "Dans l'épisode précédent" in contents:
            return "Dans l'épisode précédent de Camp… Max est en ville."
        raise AssertionError(contents[:80])


@pytest.fixture
def camp(tmp_path, monkeypatch, cfg_file):
    monkeypatch.setitem(SET.values, "campagnes.dossier", str(tmp_path / "camp"))
    monkeypatch.setitem(SET.values, "sessions.dossier", str(tmp_path / "sessions"))
    store = campagne.CampaignStore(tmp_path / "camp")
    c = store.create("Camp", "Pathfinder", MJ, [MJ, P1])
    j = c.joueurs()
    j["joueurs"][0]["personnage"] = "Max le Rouge"
    c.save_joueurs(j)
    fake = FakeGemini()
    monkeypatch.setattr(memoire.gemini, "generate", fake)
    monkeypatch.setitem(SET.values, "regles.cle_api", "k")
    monkeypatch.setitem(SET.values, "campagnes.resume_auto", True)
    return c, fake


ROWS = [
    {"user": "1", "name": "couette_", "start": 0, "end": 3, "text": "Vous arrivez devant la porte."},
    {"user": "2", "name": "silentgob_", "start": 4, "end": 5, "text": "merci"},
    {"user": "2", "name": "silentgob_", "start": 6, "end": 8, "text": "J'ouvre la porte."},
    {"user": "2", "name": "silentgob_", "start": 9, "end": 10, "text": "doucement."},
    {"user": "9", "name": "inconnu", "start": 2000, "end": 2003, "text": "Un autre passage."},
]


def test_nettoyage_etiquettes_par_id_sans_fusion_par_defaut(camp):
    c, _ = camp
    rows = memoire.clean_rows(ROWS, memoire.labeler(c.joueurs()))
    assert [(r[0], r[2], r[3]) for r in rows] == [
        (0, "MJ", "Vous arrivez devant la porte."),
        (4, "Max le Rouge (Silent)", "merci"),                # conservé : seuls les sons sans sens sont retirés
        (6, "Max le Rouge (Silent)", "J'ouvre la porte."),    # une réplique = une ligne, horodatage d'origine
        (9, "Max le Rouge (Silent)", "doucement."),
        (2000, "inconnu", "Un autre passage.")]
    assert len(memoire.windows(rows, 30)) == 2


def test_reponses_et_paroles_simultanees_preservees(camp):
    c, _ = camp
    rows = [{"user": "1", "name": "mj", "start": 10, "end": 18, "text": "Tu veux attaquer ?"},
            {"user": "2", "name": "p", "start": 15, "end": 16, "text": "Non."},               # réponse : à garder
            {"user": "2", "name": "p", "start": 16.5, "end": 17, "text": "euh"},             # son sans sens : retiré
            {"user": "1", "name": "mj", "start": 17, "end": 20, "text": "D'accord, tour suivant."}]
    out = memoire.clean_rows(rows, memoire.labeler(c.joueurs()))
    assert [(r[0], r[2], r[3]) for r in out] == [(10, "MJ", "Tu veux attaquer ?"), (15, "Max le Rouge (Silent)", "Non."),
                                                  (17, "MJ", "D'accord, tour suivant.")]


def test_fusion_optionnelle_jamais_par_dessus_un_autre_locuteur(camp, monkeypatch):
    c, _ = camp
    rows = [{"user": "1", "name": "mj", "start": 0, "end": 2, "text": "Un."},
            {"user": "1", "name": "mj", "start": 3, "end": 4, "text": "Deux."},
            {"user": "2", "name": "p", "start": 4.5, "end": 5, "text": "Attends."},
            {"user": "1", "name": "mj", "start": 5.5, "end": 6, "text": "Trois."}]
    out = memoire.clean_rows(rows, memoire.labeler(c.joueurs()), gap_merge_s=2)
    assert [(r[0], r[3]) for r in out] == [(0, "Un. Deux."), (4.5, "Attends."), (5.5, "Trois.")]


def test_parse_formats_de_transcript():
    bot = "# en-tête\n[00:00:01.500 → 00:00:03.000] silentgob : Bonjour"
    srt = "1\n00:00:01,000 --> 00:00:02,000\ncouette : Salut"
    assert memoire.parse_transcript_text(bot)[0]["name"] == "silentgob" and memoire.parse_transcript_text(bot)[0]["start"] == 1.5
    assert memoire.parse_transcript_text(srt)[0]["text"] == "Salut"


def test_cascade_complete_et_fichiers(camp):
    c, fake = camp
    steps = []
    out = memoire.summarize_session(c, ROWS, "2026-10-30_200000", steps.append)
    assert steps[0] == "extraction 1/2" and steps[-1] == "« précédemment »"
    assert len(fake.calls) == 2 + 3                                       # 2 tranches + CR + état + précédemment
    seance = c.path / "seances" / "2026-10-30_200000"
    for name in ("transcript_nettoye.txt", "extractions.md", "compte_rendu.md", "precedemment.md", "meta.json"):
        assert (seance / name).exists()
    assert "30/10/2026" in fake.calls[2]                                  # date dans la consigne du compte rendu
    assert "Max le Rouge (Silent)" in fake.calls[0]
    assert out["version"] == "v001_2026-10-30-200000.md" and c.resume_global().read_text(encoding="utf-8").startswith("# État")
    assert memoire.latest_precedemment(c).startswith("Dans l'épisode précédent")
    with pytest.raises(campagne.CampaignError, match="déjà intégrée"):
        memoire.summarize_session(c, ROWS, "2026-10-30_200000")


def test_echec_gemini_ne_touche_pas_au_resume_global(camp, monkeypatch):
    c, _ = camp
    campagne.add_version(c, "# État initial", "journal")
    def boom(instruction, contents, **kw):
        if "écrire le compte rendu" in contents:
            raise campagne.GeminiError("504")
        return "ok"
    monkeypatch.setattr(memoire.gemini, "generate", boom)
    with pytest.raises(campagne.GeminiError):
        memoire.summarize_session(c, ROWS, "s1")
    assert c.resume_global().read_text(encoding="utf-8") == "# État initial\n" and len(campagne.versions(c)) == 1
    assert not (c.path / "seances" / "s1" / "meta.json").exists()


def test_rollback_retire_la_seance_et_son_precedemment(camp):
    c, _ = camp
    campagne.add_version(c, "# journal", "journal")
    memoire.summarize_session(c, ROWS, "2026-10-30_200000")
    assert memoire.latest_precedemment(c) and c.nb_seances() == 1
    undone, now, gone = campagne.rollback(c)
    assert gone == "2026-10-30_200000" and now.startswith("v001")
    assert memoire.latest_precedemment(c) is None and c.nb_seances() == 0
    assert (c.path / "seances" / "_annulees" / "2026-10-30_200000" / "compte_rendu.md").exists()
    memoire.summarize_session(c, ROWS, "2026-10-30_200000")               # on peut refaire la séance après rollback
    assert len(campagne.versions(c)) == 2


# --- pipeline -----------------------------------------------------------------------------------

class Chan:
    async def send(self, content, allowed_mentions=None):
        self.msg = types.SimpleNamespace(versions=[content], edit=self._edit)
        return self.msg

    async def _edit(self, content, allowed_mentions=None): self.msg.versions.append(content)


def run_memoire_step(rec):
    from voicebot import pipeline
    async def main():
        board = StatusBoard(Chan(), asyncio.get_running_loop(), "t", PIPELINE_STEPS)
        res = {"notes": []}
        out = await asyncio.to_thread(pipeline._memoire_step, rec, res, board)
        return out, res, board.state["memoire"]
    return asyncio.run(main())


def fake_rec(tmp_path, with_campaign):
    d = tmp_path / "s" / "2026-10-30_200000"
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript_live.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS), encoding="utf-8")
    (d / "campagne.json").unlink(missing_ok=True)
    if with_campaign:
        (d / "campagne.json").write_text(json.dumps({"campagne": "camp"}), encoding="utf-8")
    return types.SimpleNamespace(dir=d, session_id=d.name)


def test_etape_pipeline(camp, tmp_path):
    c, _ = camp
    out, res, state = run_memoire_step(fake_rec(tmp_path, True))
    assert state[0] == "ok" and out["titre"] == "Camp" and out["compte_rendu"].startswith("# Compte rendu")


def test_etape_ignoree_sans_campagne_cle_ou_option(camp, tmp_path, monkeypatch):
    assert run_memoire_step(fake_rec(tmp_path, False))[2] == ("skipped", "aucune campagne active")
    monkeypatch.setitem(SET.values, "regles.cle_api", "")
    assert run_memoire_step(fake_rec(tmp_path, True))[2] == ("skipped", "pas de clé Gemini")
    monkeypatch.setitem(SET.values, "campagnes.resume_auto", False)
    assert run_memoire_step(fake_rec(tmp_path, True))[2] == ("skipped", "à la demande : /campagne pack")


def test_etape_echec_gemini_non_bloquante(camp, tmp_path, monkeypatch):
    monkeypatch.setattr(memoire.gemini, "generate", lambda *a, **k: (_ for _ in ()).throw(campagne.GeminiError("quota")))
    out, res, state = run_memoire_step(fake_rec(tmp_path, True))
    assert out is None and state[0] == "failed" and "/campagne pack" in res["notes"][0]


# --- commandes ---------------------------------------------------------------------------------

class Att:
    def __init__(self, text): self._b, self.size = text.encode(), len(text.encode())
    async def read(self): return self._b


def test_commande_resumer_avec_transcript_joint(camp, monkeypatch):
    c, _ = camp
    monkeypatch.setitem(SET.values, "campagnes.moteur_resume", "gemini")          # ancienne méthode, toujours disponible
    bot, inter = make_bot(), FakeInter()
    inter.channel_id = 1
    cmd = bot.tree.commands["campagne"]
    asyncio.run(cmd(inter, types.SimpleNamespace(value="resumer"), session="2026-10-24_190000",
                    fichier=Att("[00:00:01.000 → 00:00:04.000] couette : Vous entrez dans la salle.")))
    assert any("intégrée" in str(o) for o in inter.out) and c.nb_seances() == 1
    assert len(campagne.versions(c)) == 1


def test_record_affiche_le_precedemment(camp):
    c, _ = camp
    memoire.summarize_session(c, ROWS, "2026-10-30_200000")
    assert memoire.latest_precedemment(c) == "Dans l'épisode précédent de Camp… Max est en ville."
