"""Packs de prompts à coller dans une interface de chat (Gemini, Claude...) et intégration de la réponse.

Aucun appel d'API : le bot prépare des fichiers, l'humain les colle dans le chat de son choix, relit la réponse puis la
réinjecte avec /campagne integrer. Module indépendant de Discord.

Une séance longue est découpée en parties (campagnes.pack_minutes). Chaque partie produit des notes ; la dernière
partie produit en plus les livrables (compte rendu, changements, paragraphe de chronologie, rubriques modifiées, « précédemment »). Chaque réponse commence par `=== FIN LUE === hh:mm:ss` : l'horodatage de
la dernière réplique réellement lue, comparé à l'horodatage attendu pour détecter une lecture tronquée.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from . import fusion
from .campagne import Campaign, CampaignError, add_version, slugify
from .config import SET
from .memoire import (ETAT_VIDE, longueur_precedemment, REGLES_COMMUNES, clean_rows, contexte_text, etat_text, hms, labeler, render_lines,
                      seance_dirs, session_date, windows)

TOLERANCE_S = 90
NOTES_RETARD_MAX_S = 15 * 60          # les notes doivent s'arrêter à moins de 15 min de la fin attendue
RE_SESSION = re.compile(r"^\d{4}-\d{2}-\d{2}(_\d{6})?$")

STRUCTURE_ETAT = """Si l'état de la campagne ci-dessus est vide, donne dans SECTIONS toutes ces rubriques : « Personnages joueurs : situation actuelle », « Alliés, adversaires et PNJ », « Lieux », « Fils ouverts », « Réserves »."""

TACHE_NOTES = """TÂCHE : extraire les événements de jeu de cette partie de la transcription.
Liste à puces chronologique. Chaque puce : l'horodatage [hh:mm:ss] du début du fait, ce qui se passe (qui fait quoi, résultat) et un indice de confiance ● sûr / ◐ probable / ○ incertain. Marque [secret:X] les faits secrets. Regroupe les combats en quelques puces (déroulé, issue, pertes importantes) sans détailler chaque jet de dés. Termine par « Noms douteux » et « Fils ouverts » (questions posées, indices, décisions annoncées). Aucun résumé du hors-jeu."""

TACHE_LIVRABLES = """TÂCHE FINALE : à partir de la transcription (et de tes notes si la séance était en plusieurs parties), produis les documents ci-dessous. Ne recopie JAMAIS l'état actuel de la campagne : donne seulement ce qui change, le programme fait lui-même la fusion.

=== COMPTE RENDU ===
Compte rendu de la séance du {date}, destiné à TOUS les joueurs. Structure : titre avec la date ; résumé en une phrase ; rappel de départ (reprendre le récap du MJ s'il existe) ; déroulé par grandes scènes (un court paragraphe chacune) ; « Où on en est » ; « Points ouverts » ; « Réserves » (noms à confirmer, parties peu fiables). 350 à 500 mots. Écarte tous les faits [secret:X] et [mj]. Utilise les noms des personnages, pas ceux des joueurs.

=== CHANGEMENTS ===
3 à 8 puces très courtes : ce que cette séance ajoute, corrige ou contredit par rapport à l'état actuel de la campagne.

=== CHRONOLOGIE ===
UNIQUEMENT le texte de la séance du {date}, qui sera ajouté à la fin de la rubrique « Chronologie » de l'état actuel. Même forme, même niveau de détail et même style de titre que les séances déjà présentes dans cette rubrique (si elle est vide : un titre « ### Séance du {date} » puis des puces). Ne recopie aucune séance antérieure.

=== SECTIONS ===
Seulement les rubriques de l'état actuel dont le contenu DOIT changer (situation des personnages, alliés / adversaires / PNJ, lieux, fils ouverts, réserves…), jamais « Chronologie » ni « Changements ». Pour chacune : une ligne `--- SECTION: titre exact de la rubrique ---`, puis son contenu COMPLET et à jour, sans la ligne de titre. Une rubrique qui ne change pas n'apparaît pas. Supprime les fils résolus, ajoute les nouveaux ; si la séance contredit l'état actuel, ne tranche pas : note la contradiction dans « Réserves ». Rubriques de l'état actuel : {rubriques}. Même règle de secret : ce document est lu par tous les joueurs. {structure}

=== PRECEDEMMENT ===
Le « Dans l'épisode précédent… » lu à voix haute au début de la prochaine séance. Il raconte UNIQUEMENT la séance du {date} : les séances antérieures ne servent qu'à comprendre (une demi-phrase au plus, si c'est indispensable). Ton de récap de série, vivant mais sobre, sans plaisanterie forcée, sans exagération. N'invente rien : tout doit figurer dans le compte rendu. {mots}, 2 paragraphes courts, puis une phrase d'accroche sur les enjeux immédiats. Commence par « Dans l'épisode précédent de {campagne}… ». Respecte les secrets."""

FORMAT_REPONSE = """FORMAT DE RÉPONSE : commence par la ligne exacte `=== FIN LUE === hh:mm:ss | « texte »`, où hh:mm:ss est l'horodatage de la TOUTE DERNIÈRE réplique que tu as réellement lue dans cette partie et « texte » cette réplique recopiée MOT POUR MOT (ne devine rien : si le texte te semble coupé, donne la dernière réplique que tu as vraiment lue). Puis la ligne `=== NOTES ===` suivie des notes.{suite}"""


def _tag(name: str, body: str) -> str:
    return f"<{name}>\n{body.strip()}\n</{name}>"


def build_packs(campaign: Campaign, rows: list, session_id: str, part_minutes: int | None = None) -> dict:
    """Écrit les packs dans seances/<session>/pack/ et pack.json. -> {'fichiers': [Path], 'parties': [{...}]}."""
    cleaned = clean_rows(rows, labeler(campaign.joueurs()), SET["campagnes.pack_fusion_s"])       # pack allégé
    if not cleaned:
        raise CampaignError("Aucune parole exploitable dans cette séance.")
    minutes = SET["campagnes.pack_minutes"] if part_minutes is None else part_minutes
    parts = windows(cleaned, minutes) if minutes > 0 else [cleaned]          # 0 : un seul pack
    n, date = len(parts), session_date(session_id)
    contexte, etat = contexte_text(campaign), etat_text(campaign)
    out_dir = campaign.path / "seances" / session_id / "pack"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("pack_*.md"):
        old.unlink()
    files, infos = [], []
    for i, part in enumerate(parts, 1):
        first, last = i == 1, i == n
        header = (f"Séance du {date}, partie {i} sur {n} ({hms(part[0][0])} → {hms(part[-1][0])})." if n > 1
                  else f"Séance du {date}.")
        blocks = [REGLES_COMMUNES.strip()]
        if first or last:
            blocks += [_tag("contexte", contexte), _tag("etat_campagne", etat)]
        if n > 1 and not first:
            blocks.append("Cette partie fait suite aux précédentes, envoyées dans CETTE MÊME conversation : garde leurs notes en mémoire.")
        blocks += [header, _tag("transcription", render_lines(part)), TACHE_NOTES]
        suite = ""
        if last:
            rubriques = ", ".join(f"« {t} »" for t in fusion.titles(etat)) or "(aucune : l'état est vide ou en texte libre)"
            blocks.append(TACHE_LIVRABLES.format(mots=longueur_precedemment(), date=date, campagne=campaign.titre,
                                                 rubriques=rubriques, structure=STRUCTURE_ETAT))
            suite = " Puis, après les notes, les documents décrits plus haut, chacun introduit par sa ligne exacte : `=== COMPTE RENDU ===`, `=== CHANGEMENTS ===`, `=== CHRONOLOGIE ===`, `=== SECTIONS ===` puis `=== PRECEDEMMENT ===`."
        blocks.append(FORMAT_REPONSE.format(suite=suite))
        name = f"pack_{session_id}.md" if n == 1 else f"pack_{session_id}_partie_{i}_sur_{n}.md"
        (out_dir / name).write_text("\n\n".join(b for b in blocks if b) + "\n", encoding="utf-8")
        files.append(out_dir / name)
        infos.append({"fichier": name, "debut": hms(part[0][0]), "fin_attendue": hms(part[-1][0]),
                      "fin_attendue_s": int(part[-1][0]), "repliques": len(part), "derniere": part[-1][3],
                      "dernieres": [{"s": int(r[0]), "texte": r[3]} for r in part[-15:]]})
    (out_dir / "pack.json").write_text(json.dumps(
        {"session": session_id, "parties": infos, "creee_le": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"fichiers": files, "parties": infos}


# --- réponse ------------------------------------------------------------------------------------

def _section(text: str, name: str, following: list) -> str | None:
    stop = "|".join(re.escape(f) for f in following) or r"\Z"
    m = re.search(rf"^[#*\s]*===\s*{re.escape(name)}\s*===[#*\s]*$\n?(.*?)(?=^[#*\s]*===\s*(?:{stop})\s*===|\Z)",
                  text, re.S | re.M | re.I)
    return m.group(1).strip() if m else None


def parse_reponse(text: str) -> dict:
    """-> {'fin_lue_s', 'notes', 'compte_rendu', 'precedemment', 'resume_global' (ancien format) ou 'delta'}.

    Deux formats : le nouveau (`CHRONOLOGIE`, `SECTIONS`, `CHANGEMENTS` : seulement ce qui change) et l'ancien
    (`RESUME GLOBAL` : document complet), toujours accepté. Lève CampaignError si incomplet."""
    names = ["FIN LUE", "NOTES", "COMPTE RENDU", "CHANGEMENTS", "CHRONOLOGIE", "SECTIONS", "RESUME GLOBAL", "PRECEDEMMENT"]
    text = text.replace("\r\n", "\n")
    out = {"notes": _section(text, "NOTES", names) or ""}
    for key, name in (("compte_rendu", "COMPTE RENDU"), ("resume_global", "RESUME GLOBAL"), ("precedemment", "PRECEDEMMENT")):
        out[key] = _section(text, name, names)
    chrono = _section(text, "CHRONOLOGIE", names)
    out["delta"] = None
    required = [("compte_rendu", "COMPTE RENDU"), ("precedemment", "PRECEDEMMENT")]
    if out["resume_global"]:
        pass                                                      # ancien format : document complet
    elif chrono:
        out["delta"] = {"chronologie": chrono, "changements": _section(text, "CHANGEMENTS", names) or "",
                        "sections": fusion.parse_sections(_section(text, "SECTIONS", names) or "")}
    else:
        required.append(("resume_global", "CHRONOLOGIE"))
    missing = [n for k, n in required if not out.get(k)]
    if missing:
        raise CampaignError("Réponse incomplète : sections manquantes (" + ", ".join(missing) + "). "
                            "Demande au modèle de les produire avec les lignes `=== … ===` exactes.")
    out["fin_lue_s"], out["fin_lue_texte"] = None, ""
    line = re.search(r"^[#*\s]*=== *FIN LUE *===(.*)$", text, re.M | re.I)
    if line:
        m = re.search(r"\[?(\d+):(\d\d):(\d\d)\]?", line.group(1))
        if m:
            out["fin_lue_s"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            out["fin_lue_texte"] = re.sub(r"^[\s|:\-–—«»\"“”*]+|[\s«»\"“”*]+$", "", line.group(1)[m.end():])
    stamps = [int(h) * 3600 + int(mi) * 60 + int(sec) for h, mi, sec in re.findall(r"\[(\d+):(\d\d):(\d\d)\]", out["notes"])]
    out["notes_fin_s"] = max(stamps) if stamps else None
    return out


def _norm(text: str) -> str:
    return slugify(text).replace("-", " ")


def controle_lecture(data: dict, expected: dict | None) -> list:
    """Avertissements sur la lecture du modèle : horodatage, citation de la dernière réplique, étendue des notes."""
    if not expected:
        return []
    warns, fin = [], expected["fin_attendue"]
    if data["fin_lue_s"] is None:
        warns.append(f"La ligne `=== FIN LUE ===` est absente : impossible de vérifier que le modèle a lu toute la séance (fin attendue {fin}).")
    elif abs(data["fin_lue_s"] - expected["fin_attendue_s"]) > TOLERANCE_S:
        warns.append(f"Le modèle dit avoir lu jusqu'à {hms(data['fin_lue_s'])}, la fin attendue est {fin} : lecture probablement tronquée.")
    quote = _norm(data.get("fin_lue_texte", ""))
    if data["fin_lue_s"] is not None:
        last = [_norm(r["texte"]) for r in expected.get("dernieres", [])]
        if len(quote.split()) < 2:
            warns.append("La dernière réplique n'est pas recopiée dans `=== FIN LUE ===` : contrôle impossible.")
        elif last and not any(quote in t or (len(t.split()) >= 2 and t in quote) for t in last):
            warns.append(f"La réplique citée (« {data['fin_lue_texte'][:60]} ») ne figure pas parmi les dernières du transcript "
                         f"(« {expected.get('derniere', '')[:60]} ») : le modèle n'a probablement pas tout lu.")
    if data.get("notes_fin_s") is not None and data["notes_fin_s"] < expected["fin_attendue_s"] - NOTES_RETARD_MAX_S:
        warns.append(f"Les notes s'arrêtent à {hms(data['notes_fin_s'])}, loin de la fin attendue ({fin}) : la fin de la séance est peut-être absente.")
    return warns


def pending_session(campaign: Campaign) -> str | None:
    """Dernière séance dont un pack existe et qui n'est pas encore intégrée."""
    for d in reversed(seance_dirs(campaign)):
        if (d / "pack" / "pack.json").exists() and not (d / "meta.json").exists():
            return d.name
    return None


def trop_long(precedemment: str) -> str | None:
    """Avertissement si le « précédemment » dépasse nettement la longueur visée (`voix.mots`)."""
    n, mots = int(SET["voix.mots"]), len(precedemment.split())
    if mots > n * 1.35:
        return f"Le « précédemment » fait {mots} mots (visé : {n}) : c'est long à dire à voix haute."
    return None


def integrate(campaign: Campaign, answer: str, session_id: str | None = None, source: str = "pack",
              extra: dict | None = None) -> dict:
    """Range la réponse : compte rendu, « précédemment », nouvelle version du résumé global.

    Nouveau format : le résumé global est la version actuelle + le paragraphe de chronologie + les rubriques modifiées
    (rien d'autre n'est réécrit). Ancien format : le document complet fourni remplace le résumé.
    `source`/`extra` : traces écrites dans meta.json. -> {'version', 'seance', 'avertissement' (str ou None), ...}."""
    session_id = session_id or pending_session(campaign)
    if not session_id:
        raise CampaignError("Aucune séance en attente : génère d'abord un pack avec `/campagne pack`.")
    seance = campaign.path / "seances" / session_id
    if (seance / "meta.json").exists():
        raise CampaignError(f"La séance {session_id} est déjà intégrée : fais d'abord `/campagne rollback` si elle est la dernière.")
    data = parse_reponse(answer)
    try:
        expected = json.loads((seance / "pack" / "pack.json").read_text(encoding="utf-8"))["parties"][-1]
    except (OSError, ValueError, KeyError, IndexError):
        expected = None
    warns = controle_lecture(data, expected)
    redo = ("Fais `/campagne rollback` puis relance `/campagne resumer`" if source == "claude"
            else "Relance la dernière partie, puis `/campagne rollback`")
    warning = ("\n".join(warns) + f"\n{redo} avant de te fier à cette version.") if warns else ""
    if data["delta"]:
        current = campaign.resume_global()
        resume, notes = fusion.merge(current.read_text(encoding="utf-8") if current else "", data["delta"]["chronologie"],
                                     data["delta"]["sections"], data["delta"]["changements"],
                                     document_title=f"Résumé global : {campaign.titre}")
    else:
        resume, notes = data["resume_global"], []
    long_warning = trop_long(data["precedemment"])
    notes = notes + ([long_warning] if long_warning else [])
    warning = "\n".join(x for x in [warning, *notes] if x) or None
    seance.mkdir(parents=True, exist_ok=True)
    (seance / "reponse.md").write_text(answer.rstrip() + "\n", encoding="utf-8")
    (seance / "extractions.md").write_text(data["notes"] + "\n", encoding="utf-8")
    (seance / "compte_rendu.md").write_text(data["compte_rendu"] + "\n", encoding="utf-8")
    (seance / "precedemment.md").write_text(data["precedemment"] + "\n", encoding="utf-8")
    version = add_version(campaign, resume, session_id)
    meta = {"version": version.name, "source": source, "creee_le": datetime.now().isoformat(timespec="seconds"),
            **(extra or {})}
    (seance / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"version": version.name, "seance": seance, "compte_rendu": data["compte_rendu"],
            "precedemment": data["precedemment"], "avertissement": warning}


def resume_with_claude(campaign: Campaign, rows: list, session_id: str, progress=None) -> dict:
    """Pack complet (une seule partie) -> Claude -> intégration automatique. Appel bloquant (à lancer dans un thread).

    Annulable ensuite avec `/campagne rollback`. -> comme `integrate`, plus 'reponse' (claude_cli.Reponse) et 'meta'."""
    from . import claude_cli
    say = progress or (lambda _t: None)
    seance = campaign.path / "seances" / session_id
    if (seance / "meta.json").exists():
        raise CampaignError(f"La séance {session_id} est déjà intégrée : fais d'abord `/campagne rollback` si elle est la dernière.")
    say("préparation du pack")
    made = build_packs(campaign, rows, session_id, 0)
    say("Claude lit la séance et rédige")
    try:
        rep = claude_cli.ask(made["fichiers"][0].read_text(encoding="utf-8"))
    except claude_cli.ClaudeError as e:
        raise CampaignError(str(e)) from e
    extra = {"moteur": "claude", "modele": rep.modele, "effort": SET["claude.effort"], "duree_s": round(rep.duree_s, 1),
             "tokens_entree": rep.tokens_entree, "tokens_sortie": rep.tokens_sortie, "cout_usd_estime": rep.cout_usd}
    say("intégration")
    try:
        out = integrate(campaign, rep.texte, session_id, source="claude", extra=extra)
    except CampaignError:
        seance.mkdir(parents=True, exist_ok=True)
        (seance / "reponse_echec.md").write_text(rep.texte + "\n", encoding="utf-8")          # rien n'est perdu
        raise
    return {**out, "reponse": rep}
