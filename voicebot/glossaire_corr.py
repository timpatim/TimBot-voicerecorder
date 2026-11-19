"""Correction du glossaire après coup (`/glossaire corriger`) : « je m'étais trompé, ce n'est pas ce personnage ».

Un appel à Claude reçoit le glossaire numéroté et la précision du joueur, et renvoie des OPÉRATIONS (supprimer une entrée,
remplacer une entrée, en ajouter une), jamais un glossaire réécrit. Le programme les vérifie, les montre en aperçu, puis les applique
après validation. Chaque modification sauvegarde l'ancien glossaire dans `glossaire_versions/` : `annuler` le restaure.
La précision elle-même est ajoutée au glossaire comme décision datée. Seuls les analyses à venir en profitent : les comptes
rendus déjà intégrés ne sont pas touchés. Module indépendant de Discord ; l'appel à Claude est bloquant (à lancer dans un thread).
"""
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import claude_cli, glossaire
from .campagne import Campaign, CampaignError
from .memoire import REGLES_COMMUNES

DOSSIER = "glossaire_versions"
ANNULEES = "annulees"
ALERTE_PART = 0.5            # au-delà, l'aperçu avertit qu'une grande part du glossaire change
MAX_PRECISION = 1500

TACHE = """TÂCHE : mettre à jour le glossaire de la campagne après une CORRECTION d'un joueur.
Le glossaire (balise <glossaire>) garde en mémoire des noms de personnages et de lieux, des variantes mal reconnues par la transcription et des décisions prises par les joueurs en réponse à des questions de scénario. Un joueur vient de signaler qu'une information était FAUSSE ou mal comprise (balise <precision>) : sa correction fait foi.
Liste les opérations nécessaires, une par ligne, au format exact :
`G07 :: SUPPRIMER :: raison courte`
`G09 :: REMPLACER :: nouveau texte complet de l'entrée, sans le tiret initial :: raison courte`
`AJOUTER :: texte de la nouvelle entrée :: raison courte`
• Touche UNIQUEMENT les entrées démenties par la précision, ou celles qui reposent sur l'erreur (une conséquence de la mauvaise identification). Laisse toutes les autres intactes.
• REMPLACER garde le format de l'entrée : « Forme : variante, variante » pour un terme, « date · qui · question → réponse » pour une décision.
• N'ajoute pas la précision elle-même : le programme la note déjà comme décision. AJOUTER sert seulement à une entrée dérivée utile (un terme corrigé, par exemple).
• Si la précision ne change aucune entrée, écris « aucune ».
Format, deux sections :
=== OPERATIONS ===
=== RESUME ===
(une à trois phrases : ce qui change et pourquoi)"""


def entrees(campaign_dir: Path) -> list:
    """Entrées du glossaire (lignes qui commencent par « - ») : [(numéro, texte sans le tiret)], numéros stables d'une lecture à l'autre."""
    try:
        lines = glossaire.path(campaign_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [(n, x[2:].strip()) for n, x in enumerate((x for x in lines if x.startswith("- ")), 1)]


def affichage(items: list) -> str:
    return "\n".join(f"`G{n:02d}` {text}" for n, text in items) or "(le glossaire est vide)"


def prompt(campaign: Campaign, items: list, precision: str, par: str) -> str:
    try:
        contexte = campaign.contexte_path.read_text(encoding="utf-8").strip()
    except OSError:
        contexte = ""
    glo = "\n".join(f"G{n:02d} {text}" for n, text in items) or "(vide)"
    return "\n\n".join([REGLES_COMMUNES.strip(), f"<contexte>\n{contexte or '(aucun)'}\n</contexte>",
                        f"<glossaire>\n{glo}\n</glossaire>", f"<precision auteur=\"{par}\">\n{precision}\n</precision>", TACHE])


def parse_operations(text: str) -> tuple:
    """-> (opérations, résumé). Opération : {'op': SUPPRIMER|REMPLACER|AJOUTER, 'num': int|None, 'texte': str, 'raison': str}."""
    body = text.replace("\r\n", "\n")
    m = re.search(r"^[#*\s]*===\s*OPERATIONS\s*===\s*\n(.*?)(?=^[#*\s]*===\s*RESUME\s*===|\Z)", body, re.S | re.M | re.I)
    resume = re.search(r"^[#*\s]*===\s*RESUME\s*===\s*\n(.*)", body, re.S | re.M | re.I)
    ops = []
    for raw in (m.group(1) if m else "").splitlines():
        parts = [p.strip() for p in raw.strip().lstrip("-•*`").strip().rstrip("`").split("::")]
        head = parts[0].upper().replace(" ", "")
        num = re.fullmatch(r"G?(\d{1,4})", head)
        if num and len(parts) >= 2 and parts[1].upper() == "SUPPRIMER":
            ops.append({"op": "SUPPRIMER", "num": int(num.group(1)), "texte": "", "raison": " ".join(parts[2:])})
        elif num and len(parts) >= 3 and parts[1].upper() == "REMPLACER" and parts[2]:
            ops.append({"op": "REMPLACER", "num": int(num.group(1)), "texte": parts[2].lstrip("- ").strip(), "raison": " ".join(parts[3:])})
        elif head == "AJOUTER" and len(parts) >= 2 and parts[1]:
            ops.append({"op": "AJOUTER", "num": None, "texte": parts[1].lstrip("- ").strip(), "raison": " ".join(parts[2:])})
    return ops, (resume.group(1).strip() if resume else "")


def valider(ops: list, items: list) -> tuple:
    """-> (acceptées, rejetées, avertissements). Une opération acceptée porte 'avant' (texte actuel de l'entrée)."""
    known, seen, ok, ko, warns = dict(items), set(), [], [], []
    for op in ops:
        why = None
        if op["op"] != "AJOUTER":
            if op["num"] not in known:
                why = "entrée inexistante"
            elif op["num"] in seen:
                why = "entrée déjà traitée"
            elif op["op"] == "REMPLACER" and glossaire._norm(op["texte"]) == glossaire._norm(known[op["num"]]):
                why = "aucun changement"
        elif any(glossaire._norm(op["texte"]) == glossaire._norm(t) for t in known.values()):
            why = "déjà dans le glossaire"
        if why:
            ko.append({**op, "motif": why})
            continue
        if op["num"] is not None:
            seen.add(op["num"])
            op = {**op, "avant": known[op["num"]]}
        ok.append(op)
    if items and len(seen) / len(items) > ALERTE_PART and len(items) > 6:
        warns.append(f"Cette correction touche {len(seen)} entrées sur {len(items)} : vérifie bien l'aperçu.")
    return ok, ko, warns


def apercu(ok: list, ko: list, precision_ligne: str) -> str:
    lines = []
    for op in ok:
        if op["op"] == "SUPPRIMER":
            lines.append(f"🗑️ `G{op['num']:02d}` ~~{op['avant']}~~" + (f" — {op['raison']}" if op["raison"] else ""))
        elif op["op"] == "REMPLACER":
            lines.append(f"✏️ `G{op['num']:02d}` ~~{op['avant']}~~\n   → {op['texte']}" + (f" — {op['raison']}" if op["raison"] else ""))
        else:
            lines.append(f"➕ {op['texte']}" + (f" — {op['raison']}" if op["raison"] else ""))
    lines.append(f"📝 Précision notée : {precision_ligne}")
    return "\n".join(lines)


def ligne_precision(precision: str, par: str, now: datetime | None = None) -> str:
    when = (now or datetime.now()).strftime("%d/%m/%Y")
    return " ".join(f"{when} · {par} · Précision (correction) : {precision.strip()}".split())[:700]


def proposer(campaign: Campaign, precision: str, par: str) -> dict:
    """Appel à Claude puis vérification. -> {'ok', 'ko', 'avertissements', 'resume', 'precision_ligne', 'items'} ; rien n'est modifié."""
    precision = " ".join((precision or "").split())
    if len(precision) < 8:
        raise CampaignError("Précise la correction en une phrase au moins (qui est qui, ce qui était faux).")
    if len(precision) > MAX_PRECISION:
        raise CampaignError(f"Précision trop longue ({len(precision)} caractères, maximum {MAX_PRECISION}).")
    items = entrees(campaign.path)
    try:
        answer = claude_cli.ask(prompt(campaign, items, precision, par)).texte
    except claude_cli.ClaudeError as e:
        raise CampaignError(str(e)) from e
    ops, resume = parse_operations(answer)
    ok, ko, warns = valider(ops, items)
    return {"ok": ok, "ko": ko, "avertissements": warns, "resume": resume, "precision": precision, "par": par,
            "precision_ligne": ligne_precision(precision, par), "items": items}


def versions(campaign_dir: Path) -> list:
    d = Path(campaign_dir) / DOSSIER
    return sorted(d.glob("glossaire_*.md")) if d.is_dir() else []


def appliquer(campaign_dir: Path, proposal: dict) -> dict:
    """Applique une proposition validée par l'utilisateur ; sauvegarde l'ancien glossaire. -> {'version', 'supprimees', 'remplacees', 'ajoutees'}."""
    f = glossaire.path(campaign_dir)
    current = dict(entrees(campaign_dir))
    for op in proposal["ok"]:
        if op["num"] is not None and current.get(op["num"]) != op["avant"]:
            raise CampaignError("Le glossaire a changé depuis l'aperçu : relance `/glossaire corriger`.")
    d = Path(campaign_dir) / DOSSIER
    d.mkdir(exist_ok=True)
    used = [int(m.group(1)) for p in list(d.glob("glossaire_*.md")) + list((d / ANNULEES).glob("glossaire_*.md"))
            if (m := re.fullmatch(r"glossaire_(\d+)(?:_\d+)?", p.stem))]
    number = 1 + max(used, default=0)
    saved = d / f"glossaire_{number:03d}.md"
    if f.exists():
        shutil.copy2(f, saved)
    else:
        saved.write_text(glossaire.ENTETE, encoding="utf-8")
    lines = (f.read_text(encoding="utf-8").splitlines() if f.exists() else glossaire.ENTETE.splitlines())
    dash, kept, removed, replaced, added = [i for i, x in enumerate(lines) if x.startswith("- ")], list(lines), 0, 0, []
    num_to_line = {n: dash[n - 1] for n in current if n - 1 < len(dash)}
    drop = set()
    for op in proposal["ok"]:
        if op["op"] == "SUPPRIMER":
            drop.add(num_to_line[op["num"]])
            removed += 1
        elif op["op"] == "REMPLACER":
            old = lines[num_to_line[op["num"]]]
            tag = re.search(r"\(séance [^)]+\)\s*$", old)
            new = "- " + op["texte"] + (f" {tag.group(0)}" if tag and "(séance " not in op["texte"] else "")
            kept[num_to_line[op["num"]]] = new
            replaced += 1
        else:
            added.append(op["texte"])
    kept = [x for i, x in enumerate(kept) if i not in drop]
    f.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    decisions = [t for t in added if "·" in t] + [proposal["precision_ligne"]]
    term_lines = [t for t in added if "·" not in t]
    glossaire.ajouter(campaign_dir, glossaire.parse_termes("\n".join(term_lines)), decisions)
    return {"version": saved.name, "supprimees": removed, "remplacees": replaced, "ajoutees": len(added) + 1}


def annuler(campaign_dir: Path) -> str:
    """Restaure la dernière sauvegarde du glossaire (l'état actuel est gardé dans glossaire_versions/annulees/). -> nom de la version restaurée."""
    found = versions(campaign_dir)
    if not found:
        raise CampaignError("Rien à annuler : aucune correction du glossaire n'a été faite.")
    last, f = found[-1], glossaire.path(campaign_dir)
    trash = Path(campaign_dir) / DOSSIER / ANNULEES
    trash.mkdir(exist_ok=True)
    if f.exists():
        shutil.copy2(f, trash / f"glossaire_avant_annulation_{datetime.now():%Y%m%d_%H%M%S}.md")
    shutil.copy2(last, f)
    target = trash / last.name
    if target.exists():
        target = trash / f"{last.stem}_{datetime.now():%H%M%S}{last.suffix}"
    last.rename(target)
    return last.name
