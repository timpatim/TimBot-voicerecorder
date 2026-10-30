"""Statistiques de session : calcul à partir du transcript et rendu texte."""
import re

from .config import SET
from .transcript import read_rows
from .utils import fmt_dur


_WORD = re.compile(r"\w+(?:['’\-]\w+)*", re.UNICODE)
_STOPWORDS = frozenset("""
le la les un une des de du d l et ou mais donc or ni car que qui quoi dont où
je tu il elle on nous vous ils elles me te se ce cet cette ces mon ma mes ton ta tes son sa ses
notre votre leur leurs à a au aux en y dans sur sous par pour avec sans chez vers entre
est es suis sont été être étais était avoir ai as avons avez ont eu fait faire fais
ne pas plus jamais rien très trop bien aussi alors puis comme si oui non ok
c j m n s t qu ça cela ceci là ici voilà tout tous toute toutes même quand ensuite après avant
encore déjà juste peu beaucoup truc chose peut peuvent veux veut faut vais vas va vont dit dire
""".split())
_STOPWORDS |= frozenset(w.lower() for w in re.split(r"[,\s]+", SET["stats.mots_vides_supplementaires"]) if w)


def compute_stats(rows: list, duration_s: float, participants: dict | None = None) -> dict:
    """Statistiques d'une session à partir des segments transcrits (une ligne = un segment)."""
    participants = participants or {}
    users: dict[str, dict] = {}

    def user(uid: str, name: str) -> dict:
        u = users.get(uid)
        if u is None:
            u = users[uid] = {"id": uid, "name": name, "lines": 0, "words": 0, "speech_s": 0.0,
                              "questions": 0, "exclamations": 0, "interrupted_others": 0,
                              "interrupted_by_others": 0, "longest_run_s": 0.0, "longest_run_at": 0.0,
                              "_vocab": {}}
        return u

    segs = []
    minute_words: dict[int, int] = {}
    for r in rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        s, e, uid = float(r["start"]), float(r["end"]), str(r["user"])
        u = user(uid, participants.get(uid) or r.get("name") or uid)
        n = len(_WORD.findall(text))
        u["lines"] += 1
        u["words"] += n
        u["speech_s"] += max(0.0, e - s)
        u["questions"] += text.count("?")
        u["exclamations"] += text.count("!")
        minute_words[int(s // 60)] = minute_words.get(int(s // 60), 0) + n
        for w in _WORD.findall(text.lower()):
            w = re.split(r"['’]", w)[-1]
            if len(w) >= 4 and w not in _STOPWORDS:
                u["_vocab"][w] = u["_vocab"].get(w, 0) + 1
        segs.append((s, e, uid))
    segs.sort()

    # interruptions : un segment qui démarre alors qu'un autre intervenant parle encore (>= 300 ms de chevauchement)
    for i, (s, e, uid) in enumerate(segs):
        for j in range(i - 1, -1, -1):
            ps, pe, puid = segs[j]
            if ps < s - SET["stats.fenetre_interruption_s"]:
                break
            if puid != uid and ps < s and min(pe, e) - s >= SET["stats.chevauchement_interruption_s"]:
                users[uid]["interrupted_others"] += 1
                users[puid]["interrupted_by_others"] += 1
                break

    # plus longue intervention continue : segments successifs du même intervenant espacés de <= 3 s
    run = None                                     # [uid, début, fin]

    def close(r):
        if r:
            u = users[r[0]]
            if r[2] - r[1] > u["longest_run_s"]:
                u["longest_run_s"], u["longest_run_at"] = r[2] - r[1], r[1]

    for s, e, uid in segs:
        if run and run[0] == uid and s - run[2] <= SET["stats.pause_monologue_s"]:
            run[2] = max(run[2], e)
        else:
            close(run)
            run = [uid, s, e]
    close(run)

    # temps où quelqu'un parle (union des intervalles) et plus long silence
    merged: list = []
    for s, e in sorted((s, e) for s, e, _ in segs):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    speech_total = sum(e - s for s, e in merged)
    longest_gap = max(((merged[i + 1][0] - merged[i][1], merged[i][1]) for i in range(len(merged) - 1)),
                      default=(0.0, 0.0))

    # participants présents mais muets
    for uid, name in participants.items():
        user(str(uid), name)

    total_speech = sum(u["speech_s"] for u in users.values())
    ulist = []
    all_vocab: dict[str, int] = {}
    for u in users.values():
        vocab = u.pop("_vocab")
        for w, c in vocab.items():
            all_vocab[w] = all_vocab.get(w, 0) + c
        fav = max(vocab.items(), key=lambda kv: kv[1], default=None)
        u["name"] = u["name"].replace("`", "'")
        u["share_pct"] = round(100 * u["speech_s"] / total_speech, 1) if total_speech else 0.0
        u["words_per_min"] = round(u["words"] / (u["speech_s"] / 60), 1) if u["speech_s"] >= SET["stats.parole_min_mots_min_s"] else None
        u["words_per_line"] = round(u["words"] / u["lines"], 1) if u["lines"] else 0.0
        u["speech_s"] = round(u["speech_s"], 1)
        u["longest_run_s"] = round(u["longest_run_s"], 1)
        u["favorite_word"] = {"word": fav[0], "count": fav[1]} if fav and fav[1] >= SET["stats.mot_favori_min"] else None
        ulist.append(u)
    ulist.sort(key=lambda u: (-u["words"], u["name"].lower()))

    active = [u for u in ulist if u["words"] > 0]
    awards = []
    if len(active) >= 2:
        top = max(active, key=lambda u: u["words"])
        awards.append(("🗣️ Le plus bavard", top["name"], f"{top['words']} mots ({top['share_pct']:.0f} % de la parole)"))
        low = min(active, key=lambda u: u["words"])
        if low["words"] < top["words"]:
            awards.append(("🤫 Le plus discret", low["name"], f"{low['words']} mots"))
        paced = [u for u in active if u["words_per_min"]]
        if len(paced) >= 2:
            fast, slow = max(paced, key=lambda u: u["words_per_min"]), min(paced, key=lambda u: u["words_per_min"])
            awards.append(("⚡ Le plus rapide", fast["name"], f"{fast['words_per_min']:.0f} mots/min"))
            if slow["words_per_min"] < fast["words_per_min"]:
                awards.append(("🐢 Le plus posé", slow["name"], f"{slow['words_per_min']:.0f} mots/min"))
        cut = max(active, key=lambda u: u["interrupted_by_others"])
        if cut["interrupted_by_others"]:
            awards.append(("🙉 Le plus coupé", cut["name"], f"coupé {cut['interrupted_by_others']} fois"))
    if active:
        interrupter = max(active, key=lambda u: u["interrupted_others"])
        if interrupter["interrupted_others"]:
            awards.append(("✂️ L'interrupteur", interrupter["name"],
                           f"a coupé la parole {interrupter['interrupted_others']} fois"))
        curious = max(active, key=lambda u: u["questions"])
        if curious["questions"]:
            awards.append(("❓ Le plus curieux", curious["name"], f"{curious['questions']} question(s)"))
        excited = max(active, key=lambda u: u["exclamations"])
        if excited["exclamations"]:
            awards.append(("📣 Le plus enthousiaste", excited["name"], f"{excited['exclamations']} point(s) d'exclamation"))
        mono = max(active, key=lambda u: u["longest_run_s"])
        if mono["longest_run_s"] >= SET["stats.monologue_min_s"]:
            awards.append(("🎤 Plus long monologue", mono["name"],
                           f"{mono['longest_run_s']:.0f} s, à {fmt_dur(mono['longest_run_at'])}"))
    ghosts = [u["name"] for u in ulist if u["words"] == 0]

    busiest = None
    if duration_s >= 120 and len(minute_words) >= 2:
        m, w = max(minute_words.items(), key=lambda kv: kv[1])
        busiest = {"at_s": m * 60, "words": w}
    top_words = sorted(all_vocab.items(), key=lambda kv: (-kv[1], kv[0]))[:SET["stats.nb_mots_top"]]

    return {
        "duration_s": round(duration_s, 1),
        "participants": len(ulist),
        "lines": sum(u["lines"] for u in ulist),
        "words": sum(u["words"] for u in ulist),
        "speech_pct": round(min(100.0, 100 * speech_total / duration_s), 1) if duration_s > 0 else 0.0,
        "longest_silence": {"duration_s": round(longest_gap[0], 1), "at_s": round(longest_gap[1], 1)},
        "busiest_minute": busiest,
        "users": ulist,
        "awards": [{"title": t, "name": n, "detail": d} for t, n, d in awards],
        "ghosts": ghosts,
        "top_words": [[w, c] for w, c in top_words if c >= 2],
    }


def render_sections(stats: dict, esc=lambda s: s) -> list:
    """[(titre, texte, en_bloc_de_code)] ; `esc` protège les noms (markdown Discord)."""
    out = []
    head = [f"Durée {fmt_dur(stats['duration_s'])} · {stats['participants']} participant(s) · "
            f"{stats['words']} mots · {stats['lines']} lignes"]
    if stats["lines"]:
        sil = stats["longest_silence"]
        line = f"Quelqu'un parle {stats['speech_pct']:.0f} % du temps · silence {100 - stats['speech_pct']:.0f} %"
        if sil["duration_s"] >= SET["stats.silence_affiche_min_s"]:
            line += f" · plus long silence {fmt_dur(sil['duration_s'])} (à {fmt_dur(sil['at_s'])})"
        head.append(line)
        if stats["busiest_minute"]:
            b = stats["busiest_minute"]
            head.append(f"Minute la plus animée : {fmt_dur(b['at_s'])} ({b['words']} mots)")
    else:
        head.append("Aucune parole transcrite.")
    out.append(("📊 Session", "\n".join(head), False))

    if stats["lines"]:
        rows = [f"{'Nom':<14} {'Lignes':>6} {'Mots':>5} {'Parole':>6} {'Mots/min':>8} {'Part':>5}"]
        max_rows = SET["stats.lignes_tableau_discord"]
        for u in stats["users"][:max_rows]:
            wpm = f"{u['words_per_min']:.0f}" if u["words_per_min"] else "-"
            rows.append(f"{u['name'][:14]:<14} {u['lines']:>6} {u['words']:>5} {fmt_dur(u['speech_s']):>6} "
                        f"{wpm:>8} {u['share_pct']:>4.0f}%")
        if len(stats["users"]) > max_rows:
            rows.append(f"… +{len(stats['users']) - max_rows} autre(s)")
        out.append(("👥 Par intervenant", "\n".join(rows), True))

    fun = [f"{a['title']} : {esc(a['name'])} — {a['detail']}" for a in stats["awards"]]
    if stats["ghosts"]:
        fun.append("👻 Les silencieux : " + ", ".join(esc(g) for g in stats["ghosts"]) + " (présent(s) mais sans un mot)")
    if fun:
        out.append(("🏆 Palmarès", "\n".join(fun), False))

    vocab = []
    if stats["top_words"]:
        vocab.append("Mots les plus utilisés : " + ", ".join(f"« {w} » ×{c}" for w, c in stats["top_words"]))
    favs = [f"{esc(u['name'])} → « {u['favorite_word']['word']} » ×{u['favorite_word']['count']}"
            for u in stats["users"] if u["favorite_word"]]
    if favs:
        vocab.append("Mot favori : " + " · ".join(favs))
    if vocab:
        out.append(("💬 Vocabulaire", "\n".join(vocab), False))
    return out


def render_text(stats: dict) -> str:
    return "\n\n".join(f"{title}\n{body}" for title, body, _ in render_sections(stats)) + "\n"


def session_stats(rec: "SessionRecorder") -> dict:
    rows = read_rows(rec.dir / "transcript_live.jsonl")
    return compute_stats(rows, rec.duration_s(), {str(u): n for u, n in rec.names.items()})
