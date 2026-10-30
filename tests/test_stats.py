import json

from voicebot.stats import compute_stats, render_sections, render_text
from voicebot.transcript import read_rows, write_final_transcript
from voicebot.utils import dbfs, fmt_dur, fmt_ts


def row(uid, name, s, e, text):
    return {"user": uid, "name": name, "start": s, "end": e, "text": text, "source": "live"}


ROWS = [
    row("1", "Tim", 2.0, 6.0, "Alors, qu'est-ce que vous pensez du projet ? Le projet avance bien !"),
    row("2", "Zoé", 5.2, 9.0, "Moi je trouve que le projet manque de tests, vraiment."),      # coupe Tim
    row("1", "Tim", 9.5, 14.0, "Ok, les tests sont importants. Les tests d'abord, toujours les tests."),
    row("1", "Tim", 14.5, 22.0, "Bon, on peut avancer sur le projet, je prépare la démo pour lundi prochain."),
    row("2", "Zoé", 95.0, 99.0, "Pardon, peut-être que le projet peut attendre ?"),
    row("1", "Tim", 130.0, 131.5, "D'accord."),
]


def by_name(stats):
    return {u["name"]: u for u in stats["users"]}


def test_comptes_par_intervenant():
    s = compute_stats(ROWS, 150.0, {"1": "Tim", "2": "Zoé", "3": "Léo`x"})
    users = by_name(s)
    assert (users["Tim"]["lines"], users["Zoé"]["lines"]) == (4, 2)
    assert users["Tim"]["words"] > users["Zoé"]["words"] > 0
    assert s["lines"] == 6 and s["words"] == users["Tim"]["words"] + users["Zoé"]["words"]
    assert round(sum(u["share_pct"] for u in s["users"])) == 100


def test_interruptions_et_palmares():
    s = compute_stats(ROWS, 150.0, {"1": "Tim", "2": "Zoé"})
    users = by_name(s)
    assert users["Zoé"]["interrupted_others"] == 1 and users["Tim"]["interrupted_by_others"] == 1
    titles = {a["title"]: a["name"] for a in s["awards"]}
    assert titles["🗣️ Le plus bavard"] == "Tim" and titles["✂️ L'interrupteur"] == "Zoé"
    assert titles["❓ Le plus curieux"] == "Tim"


def test_silence_minute_animee_et_mots_favoris():
    s = compute_stats(ROWS, 150.0, {"1": "Tim", "2": "Zoé"})
    assert s["longest_silence"]["duration_s"] > 60 and s["busiest_minute"]["at_s"] == 0
    assert ["projet", 5] in s["top_words"]
    assert by_name(s)["Tim"]["favorite_word"]["word"] == "projet"


def test_participant_muet_et_noms_proteges():
    s = compute_stats(ROWS, 150.0, {"1": "Tim", "2": "Zoé", "3": "Léo`x"})
    assert s["ghosts"] == ["Léo'x"]


def test_session_vide_ou_un_seul_intervenant():
    empty = compute_stats([], 60.0, {"1": "Tim"})
    assert empty["lines"] == 0 and "Aucune parole transcrite." in render_text(empty)
    one = compute_stats(ROWS[:1], 30.0, {"1": "Tim"})
    assert not [a for a in one["awards"] if "bavard" in a["title"]]       # pas de classement à un seul


def test_rendu_texte_et_sections():
    s = compute_stats(ROWS, 150.0, {"1": "Tim", "2": "Zoé"})
    text = render_text(s)
    assert "Palmarès" in text and "Par intervenant" in text
    assert any(code for _t, _b, code in render_sections(s))              # le tableau est en bloc de code


def test_lecture_tolerante_et_transcript_final(tmp_path):
    from voicebot.audio import SessionRecorder
    rec = SessionRecorder("s", tmp_path)
    rec.stop()
    jsonl = rec.dir / "transcript_live.jsonl"
    lines = [json.dumps(r, ensure_ascii=False) for r in (ROWS[1], ROWS[0])]
    jsonl.write_text("\n".join(lines) + '\n{"user": "2", "na', encoding="utf-8")        # dernière ligne tronquée
    assert len(read_rows(jsonl)) == 2
    txt = write_final_transcript(rec).read_text(encoding="utf-8")
    body = [line for line in txt.splitlines() if line.startswith("[")]
    assert body[0].startswith("[00:00:02.000") and "Tim :" in body[0] and "Zoé :" in body[1]   # trié par temps


def test_formats():
    assert fmt_ts(3725.5) == "01:02:05.500" and fmt_dur(75) == "01:15" and fmt_dur(3725) == "1:02:05"
    assert dbfs(b"") == -120.0 and dbfs(b"\x00\x00" * 10) < -100
