"""Glossaire appris d'une campagne (`glossaire.md`) : noms et termes mal reconnus, décisions de scénario.

Fichier lisible et modifiable à la main, une entrée par ligne :

    ## Noms et termes (forme correcte : variantes mal reconnues)
    - Garmoth : Garm, Garmot
    ## Décisions de scénario (réponses des joueurs)
    - 2026-11-03 · Couette · Le garde de la scène de 01:12 est-il Garmoth ? → Oui

Les entrées ajoutées par une séance sont notées dans `seances/<séance>/analyse/glossaire_ajouts.txt` : `/campagne rollback`
les retire (et seulement elles). Le contenu est ajouté au contexte envoyé au modèle (voir `memoire.contexte_text`).
Module indépendant de Discord.
"""
import re
from pathlib import Path

FICHIER = "glossaire.md"
AJOUTS = "glossaire_ajouts.txt"
ENTETE = ("# Glossaire appris\n\nComplète le contexte de la campagne. Une ligne par entrée, modifiable à la main ; ajouté par "
          "`/campagne resumer`, retiré par `/campagne rollback` (seulement ce que la séance annulée avait ajouté).\n")
TITRE_TERMES = "## Noms et termes (forme correcte : variantes mal reconnues)"
TITRE_DECISIONS = "## Décisions de scénario (réponses des joueurs)"
MAX_TERMES = 40                                       # par séance


def path(campaign_dir: Path) -> Path:
    return Path(campaign_dir) / FICHIER


def etiquette(session_id: str) -> str:
    """Marque ajoutée en fin de chaque décision : relie la ligne à la séance qui l'a produite."""
    return f"(séance {session_id})"


def texte(campaign_dir: Path, exclure_seance: str | None = None) -> str:
    """Contenu du glossaire sans son en-tête, ou « » s'il n'y a pas d'entrée. `exclure_seance` : ignore les décisions issues de
    cette séance (une séance ne doit jamais s'appuyer sur ses propres réponses d'un essai précédent)."""
    try:
        raw = path(campaign_dir).read_text(encoding="utf-8")
    except OSError:
        return ""
    if exclure_seance:
        raw = "\n".join(x for x in raw.splitlines() if etiquette(exclure_seance) not in x)
    body = raw.replace(ENTETE, "").strip()
    return body if any(line.startswith("- ") for line in body.splitlines()) else ""


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[\w']+", text.lower().replace("’", "'")))


def parse_termes(block: str) -> list:
    """Lignes « Forme : variante, variante » -> [(forme, [variantes])]."""
    out = []
    for line in (block or "").splitlines():
        line = line.strip().lstrip("-•*").strip()
        form, sep, variants = line.partition(":")
        form, found = form.strip(" *`"), [v.strip(" *`\"«»") for v in variants.split(",") if v.strip(" *`\"«»")]
        if sep and form and found:
            out.append((form, found))
    return out


def filtrer_termes(propositions: list, transcript: str, existant: str = "") -> list:
    """Garde les variantes réellement présentes dans le transcript (le modèle ne peut pas en inventer), retire ce qui est déjà
    au glossaire et limite le nombre d'entrées. -> [(forme, [variantes])]."""
    haystack, known, out = f" {_norm(transcript)} ", _norm(existant), []
    for form, variants in propositions:
        real = [v for v in variants if _norm(v) and _norm(v) != _norm(form) and f" {_norm(v)} " in haystack]
        real = [v for v in real if f"{_norm(form)} : " not in known or _norm(v) not in known]
        if real:
            out.append((form, real))
    return out[:MAX_TERMES]


def _insert(lines: list, heading: str, new: list) -> list:
    if heading not in lines:
        lines += ["", heading, ""]
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[:end] + new + lines[end:]


def ajouter(campaign_dir: Path, termes: list, decisions: list) -> list:
    """Ajoute des termes [(forme, [variantes])] et des décisions [texte]. -> les lignes réellement ajoutées (sans doublon)."""
    f = path(campaign_dir)
    lines = (f.read_text(encoding="utf-8").splitlines() if f.exists() else ENTETE.splitlines())
    present = {_norm(x) for x in lines}
    added_terms = [f"- {form} : {', '.join(v)}" for form, v in termes]
    added_decisions = [f"- {d.strip()}" for d in decisions if d.strip()]
    added_terms = [x for x in added_terms if _norm(x) not in present]
    added_decisions = [x for x in added_decisions if _norm(x) not in present]
    if added_terms:
        lines = _insert(lines, TITRE_TERMES, added_terms)
    if added_decisions:
        lines = _insert(lines, TITRE_DECISIONS, added_decisions)
    if added_terms or added_decisions:
        f.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return added_terms + added_decisions


def purger(campaign_dir: Path, session_id: str) -> int:
    """Retire toutes les décisions étiquetées de la séance `session_id`. -> nombre retiré."""
    f = path(campaign_dir)
    if not f.exists():
        return 0
    lines = f.read_text(encoding="utf-8").splitlines()
    kept = [x for x in lines if etiquette(session_id) not in x]
    if len(kept) != len(lines):
        f.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    return len(lines) - len(kept)


def retirer(campaign_dir: Path, seance_dir: Path, session_id: str | None = None) -> int:
    """Retire du glossaire ce que la séance avait ajouté : les lignes du relevé `seance_dir`/analyse/glossaire_ajouts.txt, et
    toutes les décisions étiquetées de `session_id` (même si le relevé manque ou si une ligne a été modifiée). -> nombre retiré."""
    f = path(campaign_dir)
    if not f.exists():
        return 0
    removed = purger(campaign_dir, session_id) if session_id else 0
    record = Path(seance_dir) / "analyse" / AJOUTS
    if not record.exists():
        return removed
    todo = [x for x in record.read_text(encoding="utf-8").splitlines() if x.strip()]
    lines = f.read_text(encoding="utf-8").splitlines()
    for entry in todo:
        if entry in lines:
            lines.remove(entry)
            removed += 1
    f.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return removed
