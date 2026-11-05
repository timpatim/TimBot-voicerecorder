"""Fusion d'un résumé global : le modèle ne renvoie que ce qui change, le programme fait la mise à jour.

Pourquoi : demander à un modèle de réécrire tout le résumé à chaque séance le dégrade peu à peu (anciennes séances
raccourcies, détails perdus). Ici, les anciennes rubriques ne sont jamais recopiées : on ajoute un paragraphe à la
« Chronologie » et on ne remplace que les rubriques que le modèle déclare modifiées.

Module indépendant de Discord. Tolère les résumés existants : titres Markdown (`## Titre`), lignes en gras seules
(`**Titre**`) ou `---- TITRE ----`, et même un texte libre sans aucun titre (importé avec `/campagne journal`).
"""
import re

from .campagne import slugify

RE_MD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
RE_AUTRES = (re.compile(r"^\*\*(.{1,80}?)\*\*\s*:?\s*$"), re.compile(r"^-{3,}\s*(.+?)\s*-{3,}\s*$"))
TITRE_CHANGEMENTS = "Changements depuis la version précédente"
TITRE_CHRONOLOGIE = "Chronologie"
RETRECISSEMENT_MIN = 0.5            # une rubrique remplacée par un texte plus court que la moitié est signalée
RETRECISSEMENT_SEUIL = 400          # (seulement si l'ancienne faisait plus de 400 caractères)


def _norm(text: str) -> str:
    return slugify(text).replace("-", " ")


def headings(lines: list) -> list:
    """-> [(numéro de ligne, niveau, titre)]. Les titres hors Markdown sont de niveau 1 s'il n'y a aucun `#`, sinon 9."""
    found, has_md = [], False
    for i, line in enumerate(lines):
        s = line.strip()
        m = RE_MD.match(s)
        if m:
            has_md = True
            found.append((i, len(m.group(1)), m.group(2).strip(" *:")))
            continue
        for rx in RE_AUTRES:
            m = rx.match(s)
            if m:
                found.append((i, 0, m.group(1).strip(" *:")))
                break
    return [(i, lvl or (9 if has_md else 1), t) for i, lvl, t in found]


def titles(text: str, max_level: int = 3) -> list:
    """Titres des rubriques d'un résumé (pour les indiquer au modèle)."""
    return [t for _i, lvl, t in headings(text.splitlines()) if lvl <= max_level]


def _find(found: list, wanted: str):
    """Rubrique `wanted` : titre exact, puis début de titre, puis mot contenu ; None si absente ou ambiguë."""
    w = _norm(wanted)
    for test in (lambda t: _norm(t) == w, lambda t: _norm(t).startswith(w), lambda t: w in _norm(t)):
        hits = [h for h in found if test(h[2])]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def _end(found: list, lines: list, hit: tuple) -> int:
    """Ligne de fin (exclue) de la rubrique : jusqu'au prochain titre de niveau égal ou supérieur."""
    for i, lvl, _t in found:
        if i > hit[0] and lvl <= hit[1]:
            return i
    return len(lines)


def _join(lines: list) -> str:
    return "\n".join(lines).strip("\n") + "\n"


def replace_section(text: str, title: str, body: str) -> tuple:
    """Remplace le contenu de la rubrique `title` (le titre est conservé) ou l'ajoute à la fin.
    -> (nouveau texte, 'remplacée' | 'ajoutée', ancien contenu ou '')."""
    lines, body = text.splitlines(), body.strip("\n")
    found = headings(lines)
    hit = _find(found, title)
    if hit is None:
        extra = ["", f"## {title}", "", *body.splitlines()] if lines else [f"## {title}", "", *body.splitlines()]
        return _join(lines + extra), "ajoutée", ""
    end = _end(found, lines, hit)
    old = "\n".join(lines[hit[0] + 1:end]).strip()
    new = lines[:hit[0] + 1] + [""] + body.splitlines() + [""] + lines[end:]
    return _join(new), "remplacée", old


def append_to_section(text: str, title: str, paragraph: str) -> tuple:
    """Ajoute `paragraph` à la fin de la rubrique `title` (créée à la fin du document si elle n'existe pas).
    -> (nouveau texte, 'complétée' | 'créée')."""
    lines, paragraph = text.splitlines(), paragraph.strip("\n")
    found = headings(lines)
    hit = _find(found, title)
    if hit is None:
        head = ["", f"## {title}", ""] if lines else [f"## {title}", ""]
        return _join(lines + head + paragraph.splitlines()), "créée"
    end = _end(found, lines, hit)
    while end > hit[0] + 1 and not lines[end - 1].strip():
        end -= 1
    return _join(lines[:end] + [""] + paragraph.splitlines() + [""] + lines[end:]), "complétée"


def set_changes(text: str, body: str) -> str:
    """Remplace (ou crée, tout en haut après le titre du document) la rubrique « Changements depuis la version précédente »."""
    lines = text.splitlines()
    found = headings(lines)
    if _find(found, TITRE_CHANGEMENTS) is not None:
        return replace_section(text, TITRE_CHANGEMENTS, body)[0]
    block = [f"## {TITRE_CHANGEMENTS}", "", *body.strip("\n").splitlines(), ""]
    at = len(lines)
    if found:
        at = found[0][0]
        if found[0][1] == 1 and at == 0 and len(found) > 1 and any(lvl >= 2 for _i, lvl, _t in found[1:]):
            at = next(i for i, lvl, _t in found[1:] if lvl >= 2)           # après le titre du document
    return _join(lines[:at] + block + lines[at:])


def merge(text: str, chronologie: str, sections: list, changements: str = "", document_title: str = "") -> tuple:
    """Fusionne la réponse du modèle dans le résumé global actuel `text` (vide = début de campagne).
    `sections` : [(titre, contenu complet)]. -> (nouveau résumé, [avertissements])."""
    warns, text = [], text.strip("\n")
    if not text.strip() and document_title:
        text = f"# {document_title}\n"
    text, how = append_to_section(text, TITRE_CHRONOLOGIE, chronologie)
    if how == "créée":
        warns.append("Le résumé actuel n'avait pas de rubrique « Chronologie » : elle a été créée à la fin avec cette séance.")
    for title, body in sections:
        if _norm(title) in (_norm(TITRE_CHRONOLOGIE), _norm(TITRE_CHANGEMENTS)):
            continue                                   # gérées à part : le modèle n'a pas à les réécrire
        text, how, old = replace_section(text, title, body)
        if how == "ajoutée":
            warns.append(f"Rubrique « {title} » absente du résumé actuel : ajoutée à la fin.")
        elif len(old) > RETRECISSEMENT_SEUIL and len(body.strip()) < RETRECISSEMENT_MIN * len(old):
            warns.append(f"Rubrique « {title} » raccourcie de {100 - 100 * len(body.strip()) // len(old)} % "
                         "(le modèle a pu perdre des détails) : compare avec la version précédente.")
    if changements.strip():
        text = set_changes(text, changements)
    return text.rstrip("\n") + "\n", warns


RE_SECTION = re.compile(r"^[#*\s-]*SECTION\s*:\s*(.+?)[*\s-]*$", re.I | re.M)


def parse_sections(block: str) -> list:
    """Découpe le bloc `=== SECTIONS ===` en [(titre, contenu)] : chaque rubrique commence par `--- SECTION: titre ---`."""
    parts = RE_SECTION.split(block.replace("\r\n", "\n"))
    return [(parts[i].strip(" *#:"), parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2) if parts[i + 1].strip()]
