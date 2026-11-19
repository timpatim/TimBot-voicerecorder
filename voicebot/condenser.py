"""Condensation de la « Chronologie » du résumé global : séances -> chapitre -> livre (`/campagne condenser`).

La rubrique « Chronologie » contient un bloc par séance (`### Séance du 03/11/2026`). Quand un épisode (exploration d'un donjon,
une mission…) s'étale sur plusieurs séances, seul son bilan compte : les informations importantes, les événements aux conséquences
durables, les personnages et lieux rencontrés, ce qui reste ouvert. Claude remplace alors des blocs CONSÉCUTIFS par un seul bloc
`### Chapitre : titre (du … au …)`. Les chapitres se condensent à leur tour en « Livre ».

Une séance charnière (elle termine un épisode et en ouvre un autre) se range avec l'épisode qu'elle termine ; le début du suivant est noté dans la section « Suite » du condensé, que le condensé suivant reprend comme amorce.

Chaque condensé devient aussi une pseudo-séance : un dossier `seances/<dernière séance couverte>_chapitre_<titre>/` avec son
`compte_rendu.md`, son `precedemment.md` (écrit par un second appel) et, côté bot, l'audio du « précédemment ». Le nom du dossier le
place juste après la dernière séance qu'il couvre : le « précédemment » utilisé est donc toujours celui de la dernière séance de la
campagne, réelle ou condensée (une séance plus récente reprend la main, à égalité le condensé gagne).

Seuls les blocs choisis sont remplacés, le reste du résumé reste identique, et le résultat est une nouvelle version du résumé global
(`/campagne rollback` l'annule). Les comptes rendus de séance ne sont jamais touchés. Module indépendant de Discord ; les appels
à Claude sont bloquants (à lancer dans un thread).
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import claude_cli, fusion
from .campagne import Campaign, CampaignError, add_version, slugify
from .memoire import REGLES_COMMUNES, contexte_text, longueur_precedemment, seance_dirs
from .pack import trop_long

NIVEAUX = ["Séance", "Chapitre", "Livre"]
MOTS_MAX = {1: 350, 2: 600}
RE_SUITE = re.compile(r"\*\*Suite\*\*\s*:?\s*(.*?)(?=\n\s*\*\*[^*\n]+\*\*|\Z)", re.S)
EXTRAIT_PLAN = 400
RE_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
RE_NOM = re.compile(r"(?<=[a-zà-ÿ,;] )[A-ZÀ-Ý][\wÀ-ÿ'’-]{2,}")

TACHE_PLAN = """TÂCHE : proposer comment condenser la chronologie de la campagne (balise <blocs>). Chaque bloc est une séance (ou un condensé déjà fait : Chapitre, Livre).
Repère les EPISODES TERMINES qui s'étalent sur plusieurs blocs consécutifs (un donjon ou un fort exploré en plusieurs séances, une mission menée à son terme, un arc dont la conclusion est atteinte) et propose de les regrouper.
• Un regroupement = des blocs CONSÉCUTIFS (au moins 2) formant un tout. Des séances se regroupent en Chapitre, des chapitres en Livre.
• Une séance CHARNIÈRE (elle conclut un épisode et en ouvre un autre) se range avec l'épisode qu'elle CONCLUT, jamais avec le suivant ni seule : la section « Suite » du condensé conservera le début du nouvel arc.
• Ne regroupe PAS un épisode encore en cours (le dernier bloc, ou un arc non conclu) ni des blocs sans lien entre eux. Mieux vaut ne rien proposer que mal regrouper.
• Un bloc ne figure que dans un seul regroupement.
Format, une ligne par regroupement, du plus ancien au plus récent :
`B03-B06 :: Titre court du chapitre :: raison en une phrase`
Écris « aucun » si rien ne se regroupe proprement. Rien d'autre."""

TACHE_CONDENSE = """TÂCHE : condenser les blocs de la balise <blocs> en un seul bloc de niveau « {niveau} ».
Ce qui compte : les informations importantes, les événements aux conséquences durables, les personnages et lieux rencontrés (avec leur rôle), les décisions, découvertes, pertes et objets importants, et ce qui reste OUVERT à la fin. Ce qui ne compte plus : le déroulé scène par scène, le détail des combats, les dialogues, les hésitations.
Règles :
• Conserve exactement les noms propres, les chiffres et les dates. N'invente rien, ne spécule pas. Parle des personnages, pas des joueurs.
• Si les blocs se contredisent, le plus récent fait foi. Un point ouvert qui a été résolu plus tard dans les blocs ne figure pas dans « Points ouverts ». Appuie-toi sur l'état actuel (balise <etat_actuel>) pour savoir ce qui est encore ouvert.
• Si une balise <amorce> est fournie, c'est la section « Suite » du bloc précédent : ce qui avait déjà commencé avant ces blocs. Intègre-le au début de ton condensé (c'est le début de cet épisode) sans le recopier tel quel.
• Longueur : environ le tiers de l'ensemble, {max} mots au maximum.
Format exact, deux sections :
=== TITRE ===
{titre_consigne}
=== TEXTE ===
**Résumé** : un paragraphe.
**Événements marquants** : puces.
**Personnages et lieux** : puces « Nom — rôle ».
**Points ouverts** : puces (ce qui n'est pas résolu à la fin).
**Suite** : ce qui COMMENCE ou se prépare à la fin des blocs et appartient à l'épisode suivant (nouvel arc, nouveaux personnages, nouveau lieu, mission annoncée), avec assez de détail pour être repris plus tard ; « aucune » si la fin est une vraie clôture.
Pas de titre dans le texte, aucune autre section."""


TACHE_PRECEDEMMENT = """TÂCHE : écrire le « Dans l'épisode précédent… » lu à voix haute au début de la prochaine séance, qui reprend juste après les séances regroupées dans la balise <condense> (un {niveau}).
Raconte l'ensemble de cet épisode dans l'ordre : le fil principal, les tournants, ce qui a changé pour les personnages. Ton de récap de série, vivant mais sobre, sans plaisanterie forcée, sans exagération. N'invente rien : tout doit figurer dans <condense> ou <derniere_seance>. {mots}, 2 à 3 paragraphes courts, puis une phrase d'accroche sur les enjeux immédiats (appuie-toi sur « Points ouverts » et « Suite », et sur <derniere_seance> si elle est fournie).
Commence par « Dans l'épisode précédent de {campagne}… ». Respecte les secrets. Réponds uniquement par ce texte."""


@dataclass
class Bloc:
    num: int
    titre: str
    niveau: int
    debut: int
    fin: int
    texte: str
    marque: str = "###"
    dates: list = field(default_factory=list)


def niveau_de(titre: str) -> int:
    first = fusion._norm(titre).split(" ")[0] if titre.strip() else ""
    return {"chapitre": 1, "partie": 1, "livre": 2}.get(first, 0)           # « partie » : ancien niveau, traité comme un chapitre


def _dates_of(text: str) -> list:
    found = sorted({(int(y), int(m), int(d)) for d, m, y in RE_DATE.findall(text)})
    return [f"{d:02d}/{m:02d}/{y}" for y, m, d in found]


def blocs(text: str) -> list:
    """Blocs de la rubrique « Chronologie » (les titres de niveau le plus haut sous la rubrique). Lève CampaignError s'il n'y en a pas."""
    lines = text.splitlines()
    found = fusion.headings(lines)
    hit = fusion._find(found, fusion.TITRE_CHRONOLOGIE)
    if hit is None:
        raise CampaignError("Le résumé global n'a pas de rubrique « Chronologie ».")
    end = fusion._end(found, lines, hit)
    subs = [h for h in found if hit[0] < h[0] < end]
    if not subs:
        raise CampaignError("La rubrique « Chronologie » ne contient aucun bloc identifiable (un titre `###` par séance est attendu).")
    base = min(h[1] for h in subs)
    starts = [h for h in subs if h[1] == base]
    out = []
    for k, (i, _lvl, title) in enumerate(starts):
        stop = starts[k + 1][0] if k + 1 < len(starts) else end
        while stop > i + 1 and not lines[stop - 1].strip():
            stop -= 1
        mark = re.match(r"\s*(#+)", lines[i])
        out.append(Bloc(k + 1, title, niveau_de(title), i, stop, "\n".join(lines[i:stop]), mark.group(1) if mark else "###",
                        _dates_of(title)))
    return out


def etat_sans_chronologie(text: str) -> str:
    lines = text.splitlines()
    found = fusion.headings(lines)
    hit = fusion._find(found, fusion.TITRE_CHRONOLOGIE)
    if hit is None:
        return text
    return "\n".join(lines[:hit[0]] + lines[fusion._end(found, lines, hit):]).strip()


def mots(text: str) -> int:
    return len(text.split())


def resume_text(campaign: Campaign) -> str:
    path = campaign.resume_global()
    if path is None:
        raise CampaignError("Cette campagne n'a pas encore de résumé global.")
    return path.read_text(encoding="utf-8")


def _ask(prompt: str) -> str:
    try:
        return claude_cli.ask(prompt).texte
    except claude_cli.ClaudeError as e:
        raise CampaignError(str(e)) from e


def _tag(name: str, body: str) -> str:
    return f"<{name}>\n{body}\n</{name}>"


# --- regroupements proposés par Claude --------------------------------------------------------------

def parse_plan(answer: str) -> list:
    out = []
    for raw in answer.splitlines():
        m = re.match(r"\s*[-•*`]*\s*B?(\d+)\s*(?:-|–|à|->|→)\s*B?(\d+)\s*::\s*(.+?)\s*(?:::\s*(.*?))?\s*`?$", raw)
        if m:
            out.append({"de": int(m.group(1)), "a": int(m.group(2)), "titre": m.group(3).strip(" *\"«»`"), "raison": (m.group(4) or "").strip()})
    return out


def valider_plan(groups: list, items: list) -> tuple:
    """-> (retenus, rejetés). Les regroupements doivent couvrir au moins 2 blocs consécutifs existants, sans chevauchement."""
    ok, ko, last = [], [], 0
    for g in sorted(groups, key=lambda x: x["de"]):
        why = None
        if not (1 <= g["de"] < g["a"] <= len(items)):
            why = "blocs inexistants ou moins de deux blocs"
        elif g["de"] <= last:
            why = "chevauche un autre regroupement"
        elif not g["titre"]:
            why = "titre manquant"
        if why:
            ko.append({**g, "motif": why})
        else:
            ok.append(g)
            last = g["a"]
    return ok, ko


def proposer(campaign: Campaign) -> dict:
    """Appel 1 (mode automatique) : regroupements proposés. -> {'groupes', 'rejetes', 'blocs'} ; rien n'est modifié."""
    items = blocs(resume_text(campaign))
    listing = "\n".join(f"B{b.num:02d} :: {b.titre} ({NIVEAUX[b.niveau]}, {mots(b.texte)} mots) :: "
                        f"{' '.join(b.texte.split(chr(10), 1)[1:]).strip()[:EXTRAIT_PLAN]}" for b in items)
    prompt = "\n\n".join([REGLES_COMMUNES.strip(), _tag("contexte", contexte_text(campaign)), _tag("blocs", listing), TACHE_PLAN])
    groups, rejected = valider_plan(parse_plan(_ask(prompt)), items)
    return {"groupes": groups, "rejetes": rejected, "blocs": items}


# --- condensation ---------------------------------------------------------------------------------

def niveau_cible(selected: list, force: int | None = None) -> int:
    """Niveau du bloc produit : un cran au-dessus du plus haut des blocs choisis (plafonné à « Livre »), sauf niveau imposé."""
    return force if force else min(2, 1 + max(b.niveau for b in selected))


def entete(niveau: int, titre: str, selected: list, marque: str = "###") -> str:
    dates = sorted({d for b in selected for d in b.dates}, key=lambda s: (s[6:], s[3:5], s[:2]))
    span = "" if not dates else (f" (le {dates[0]})" if dates[0] == dates[-1] else f" (du {dates[0]} au {dates[-1]})")
    return f"{marque} {NIVEAUX[niveau]} : {titre.strip()}{span}"


def parse_condense(answer: str) -> tuple:
    body = answer.replace("\r\n", "\n")
    t = re.search(r"^[#*\s]*===\s*TITRE\s*===\s*\n(.*?)(?=^[#*\s]*===\s*TEXTE\s*===|\Z)", body, re.S | re.M | re.I)
    x = re.search(r"^[#*\s]*===\s*TEXTE\s*===\s*\n(.*)", body, re.S | re.M | re.I)
    if not x or not x.group(1).strip():
        raise CampaignError("Claude n'a pas renvoyé la section `=== TEXTE ===` du condensé.")
    return (t.group(1).strip().splitlines()[0].strip(" *#\"«»") if t and t.group(1).strip() else ""), x.group(1).strip()


def noms_perdus(avant: str, apres: str) -> list:
    """Noms propres (mots en majuscule en milieu de phrase) cités au moins deux fois dans `avant` et absents de `apres`."""
    count: dict = {}
    for name in RE_NOM.findall(avant):
        count[name.casefold()] = count.get(name.casefold(), 0) + 1
    low = apres.casefold()
    shown = {n.casefold(): n for n in RE_NOM.findall(avant)}
    return sorted(shown[k] for k, c in count.items() if c >= 2 and k not in low)


def suite_de(text: str) -> str:
    """Section « Suite » d'un bloc condensé ('' s'il n'en a pas ou si elle dit « aucune »)."""
    m = RE_SUITE.search(text)
    out = m.group(1).strip() if m else ""
    return "" if fusion._norm(out).strip(" .") in ("", "aucune", "aucun", "neant") else out


def meta_de(d: Path) -> dict:
    try:
        return json.loads((Path(d) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def est_condense(d: Path) -> bool:
    """Vrai si le dossier de séance est un condensé (chapitre ou livre) et non une séance jouée."""
    return meta_de(d).get("type") in ("chapitre", "livre")


AVANT_TOUT = "0000-00-00_000000"        # condensé sans date et sans repère : rangé avant toutes les séances, il ne prend jamais la main


def _sid_de_date(real: list, date: str) -> str:
    iso = f"{date[6:]}-{date[3:5]}-{date[:2]}"
    same = [n for n in real if n.startswith(iso)]
    return same[-1] if same else f"{iso}_999999"


def dernier_sid(campaign: Campaign, selected: list, tous: list | None = None) -> str:
    """Identifiant de la dernière séance réelle couverte par `selected` (la plus récente date citée dans les titres des blocs) ;
    sans séance de cette date : `AAAA-MM-JJ_999999`.
    Blocs sans date (journal importé…) : le condensé est rangé d'après sa POSITION dans la chronologie `tous` : s'il va jusqu'au
    dernier bloc, il couvre la dernière séance de la campagne ; sinon il se range après le dernier bloc daté qui le précède, ou
    avant toutes les séances. Il ne passe donc jamais pour plus récent que des séances qu'il ne couvre pas."""
    real = [d.name for d in seance_dirs(campaign) if not est_condense(d)]
    key = lambda s: (s[6:], s[3:5], s[:2])                                                                     # noqa: E731
    dates = sorted({d for b in selected for d in b.dates}, key=key)
    if dates:
        return _sid_de_date(real, dates[-1])
    if tous is None or selected[-1].num >= len(tous):
        return real[-1] if real else datetime.now().strftime("%Y-%m-%d_%H%M%S")
    earlier = sorted({d for b in tous if b.num < selected[0].num for d in b.dates}, key=key)
    return _sid_de_date(real, earlier[-1]) if earlier else AVANT_TOUT


def dossier_condense(campaign: Campaign, sid: str, niveau: int, titre: str) -> Path:
    """Dossier du condensé : `<sid>_chapitre_<titre>` (trié juste après la séance `sid`, donc après elle et avant la suivante)."""
    root = campaign.path / "seances"
    base = f"{sid}_{NIVEAUX[niveau].lower()}_{slugify(titre)[:40]}".rstrip("_-")
    d, k = root / base, 1
    while d.exists():
        k += 1
        d = root / f"{base}_{k}"
    return d


def condenser_groupe(campaign: Campaign, etat: str, selected: list, titre: str | None, niveau: int | None = None,
                     amorce: str = "", tous: list | None = None) -> dict:
    """Appel : condense `selected` (blocs consécutifs). -> {'niveau', 'titre', 'entete', 'corps', 'bloc', 'avertissements', ...}."""
    cible = niveau_cible(selected, niveau)
    avant = "\n\n".join(b.texte for b in selected)
    prompt = "\n\n".join([
        REGLES_COMMUNES.strip(), _tag("contexte", contexte_text(campaign)), _tag("etat_actuel", etat_sans_chronologie(etat)),
        _tag("blocs", avant)] + ([_tag("amorce", amorce)] if amorce else []) + [
        TACHE_CONDENSE.format(niveau=NIVEAUX[cible], max=MOTS_MAX[cible],
                              titre_consigne=titre.strip() if titre else "un titre court pour cet ensemble (une ligne)")])
    proposed, corps = parse_condense(_ask(prompt))
    final_title = (titre or proposed or f"{NIVEAUX[cible]} de {len(selected)} blocs").strip()
    head = entete(cible, final_title, selected, selected[0].marque)
    warns = []
    if mots(corps) > MOTS_MAX[cible] * 1.3:
        warns.append(f"« {final_title} » : {mots(corps)} mots, plus que les {MOTS_MAX[cible]} visés.")
    if mots(corps) >= mots(avant):
        warns.append(f"« {final_title} » : le condensé n'est pas plus court que l'original ({mots(corps)} contre {mots(avant)} mots).")
    lost = noms_perdus(avant, corps)
    if lost:
        warns.append(f"« {final_title} » : noms cités plusieurs fois dans l'original et absents du condensé : {', '.join(lost[:12])}.")
    sid = dernier_sid(campaign, selected, tous)
    last_cr = campaign.path / "seances" / sid / "compte_rendu.md"
    precedemment = _ask("\n\n".join([
        REGLES_COMMUNES.strip(), _tag("condense", head + "\n\n" + corps)]
        + ([_tag("derniere_seance", last_cr.read_text(encoding="utf-8")[:4000])] if last_cr.exists() else [])
        + [TACHE_PRECEDEMMENT.format(niveau=NIVEAUX[cible], mots=longueur_precedemment(), campagne=campaign.titre)])).strip()
    long_warning = trop_long(precedemment)
    if long_warning:
        warns.append(f"« {final_title} » : {long_warning}")
    return {"niveau": cible, "niveau_nom": NIVEAUX[cible], "titre": final_title, "entete": head, "corps": corps,
            "precedemment": precedemment, "derniere_seance": sid,
            "bloc": head + "\n\n" + corps, "de": selected[0].num, "a": selected[-1].num, "blocs_avant": [b.titre for b in selected],
            "mots_avant": mots(avant), "mots_apres": mots(corps), "avertissements": warns}


def condenser(campaign: Campaign, groupes: list, progress=None) -> dict:
    """Condense chaque regroupement [{'de', 'a', 'titre'?, 'niveau'?}] (numéros de blocs, 1 = le plus ancien) puis assemble le nouveau résumé
    global. Rien n'est écrit. -> {'texte', 'groupes', 'avertissements', 'base_sha', 'mots_avant', 'mots_apres'}."""
    text = resume_text(campaign)
    items = blocs(text)
    groups, ko = valider_plan([{"titre": g.get("titre") or "?", **g} for g in groupes], items)
    if ko or not groups:
        raise CampaignError("Regroupement invalide : " + ", ".join(f"B{k['de']:02d}-B{k['a']:02d} ({k['motif']})" for k in ko) if ko
                            else "Aucun regroupement à condenser.")
    results = []
    for n, g in enumerate(groups, 1):                                   # amorce : « Suite » du bloc juste avant (ou de son condensé)
        if progress:
            progress(f"Condensation {n}/{len(groups)} : blocs {g['de']} à {g['a']}")
        sel = items[g["de"] - 1:g["a"]]
        before = next((r["bloc"] for r in results if r["a"] == g["de"] - 1), items[g["de"] - 2].texte if g["de"] > 1 else "")
        results.append(condenser_groupe(campaign, text, sel, g.get("titre") if g.get("titre") != "?" else None, g.get("niveau"),
                                        suite_de(before), items))
    lines = text.splitlines()
    for res in sorted(results, key=lambda r: r["de"], reverse=True):               # de bas en haut : les numéros de ligne restent valables
        first, last = items[res["de"] - 1], items[res["a"] - 1]
        lines[first.debut:last.fin] = res["bloc"].splitlines()
    new_text = "\n".join(lines).rstrip("\n") + "\n"
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    return {"texte": new_text, "groupes": results, "avertissements": [w for r in results for w in r["avertissements"]],
            "base_sha": hashlib.sha256(text.encode("utf-8")).hexdigest(), "mots_avant": sum(r["mots_avant"] for r in results),
            "mots_apres": sum(r["mots_apres"] for r in results)}


def compte_rendu_de(res: dict) -> str:
    """Texte du compte rendu d'un condensé : son bloc de chronologie, titre en « # »."""
    return re.sub(r"^#+\s*", "# ", res["entete"]) + "\n\n" + res["corps"]


def appliquer(campaign: Campaign, result: dict) -> dict:
    """Écrit le résumé condensé comme nouvelle version du résumé global, puis un dossier de séance par condensé (compte rendu,
    précédemment, meta.json). Refuse si le résumé a changé depuis l'aperçu.
    -> {'version': nom, 'seances': [{'seance': Path, 'precedemment', 'compte_rendu', 'avertissement', 'titre', 'niveau_nom'}]}."""
    if hashlib.sha256(resume_text(campaign).encode("utf-8")).hexdigest() != result["base_sha"]:
        raise CampaignError("Le résumé global a changé depuis l'aperçu (une séance a été intégrée ou annulée) : relance la condensation.")
    version = add_version(campaign, result["texte"], "condensation")
    made = []
    for r in result["groupes"]:
        d = dossier_condense(campaign, r["derniere_seance"], r["niveau"], r["titre"])
        d.mkdir(parents=True)
        cr = compte_rendu_de(r)
        (d / "compte_rendu.md").write_text(cr + "\n", encoding="utf-8")
        (d / "precedemment.md").write_text(r["precedemment"] + "\n", encoding="utf-8")
        meta = {"version": version.name, "type": NIVEAUX[r["niveau"]].lower(), "titre": r["titre"], "couvre": r["blocs_avant"],
                "derniere_seance": r["derniere_seance"], "creee_le": datetime.now().isoformat(timespec="seconds")}
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        made.append({"seance": d, "precedemment": r["precedemment"], "compte_rendu": cr, "avertissement": None,
                     "titre": r["titre"], "niveau_nom": r["niveau_nom"]})
    return {"version": version.name, "seances": made}


def apercu(result: dict, limite: int = 500) -> str:
    out = []
    for r in result["groupes"]:
        out.append(f"📚 **{r['niveau_nom']} : {r['titre']}** — blocs {r['de']} à {r['a']} ({len(r['blocs_avant'])} blocs, "
                   f"{r['mots_avant']} → {r['mots_apres']} mots)\n> " + r["corps"][:limite].replace("\n", "\n> ") + ("…" if len(r["corps"]) > limite else ""))
    return "\n\n".join(out)
