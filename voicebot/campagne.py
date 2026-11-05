"""Campagnes de jeu de rôle : dossier par campagne, campagne active, joueurs, contexte (généré avec Gemini).

Module indépendant de Discord. Arborescence d'une campagne :

    campagnes/<nom>/
        campagne.json     titre, jeu, date de création
        joueurs.json      MJ et joueurs (identifiants Discord, pseudos, personnage)
        contexte.md       personnages, secrets, glossaire (à relire par le MJ)
        contexte_brut.txt texte libre fourni par le MJ (source de contexte.md)
        sources/  seances/  resume_global/
    campagnes/_active.json  campagne active

Les secrets ne sont jamais déduits : Gemini propose un classement, le MJ le valide (cf. PROMPT_CONTEXTE).
"""
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from . import gemini
from .gemini import GeminiError

TEMPLATE_MARK = "<!-- modèle non rempli : supprime cette ligne quand tu as complété le fichier -->"
SUBDIRS = ("sources", "seances", "resume_global")


class CampaignError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Campaign:
    def __init__(self, path: Path):
        self.path = path
        self.slug = path.name
        self.meta = _read_json(path / "campagne.json", {})

    @property
    def titre(self) -> str:
        return self.meta.get("titre") or self.slug

    @property
    def jeu(self) -> str:
        return self.meta.get("jeu", "")

    @property
    def joueurs_path(self) -> Path:
        return self.path / "joueurs.json"

    @property
    def contexte_path(self) -> Path:
        return self.path / "contexte.md"

    @property
    def brut_path(self) -> Path:
        return self.path / "contexte_brut.txt"

    def joueurs(self) -> dict:
        return _read_json(self.joueurs_path, {"mj": {"nom": "MJ", "pseudos": []}, "joueurs": []})

    def save_joueurs(self, data: dict) -> None:
        _write_json(self.joueurs_path, data)

    def contexte_rempli(self) -> bool:
        try:
            text = self.contexte_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return bool(text.strip()) and TEMPLATE_MARK not in text

    def nb_seances(self) -> int:
        d = self.path / "seances"
        return sum(1 for p in d.iterdir() if p.is_dir() and not p.name.startswith("_")) if d.is_dir() else 0

    def resume_global(self) -> Path | None:
        p = self.path / "resume_global.md"
        return p if p.exists() else None


class CampaignStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def list(self) -> list:
        if not self.root.is_dir():
            return []
        return sorted((Campaign(p) for p in self.root.iterdir() if p.is_dir() and (p / "campagne.json").exists()),
                      key=lambda c: c.slug)

    def get(self, name: str) -> Campaign:
        """Retrouve une campagne par son nom exact, son identifiant ou un début de nom non ambigu."""
        wanted = slugify(name)
        found = self.list()
        exact = [c for c in found if c.slug == wanted or slugify(c.titre) == wanted]
        if exact:
            return exact[0]
        starts = [c for c in found if c.slug.startswith(wanted)] if wanted else []
        if len(starts) == 1:
            return starts[0]
        if len(starts) > 1:
            raise CampaignError(f"« {name} » est ambigu : " + ", ".join(c.titre for c in starts) + ".")
        raise CampaignError(f"Campagne « {name} » introuvable. Existantes : "
                            + (", ".join(c.titre for c in found) or "aucune") + ".")

    def active(self) -> Campaign | None:
        """Campagne active ; à défaut la plus récemment créée."""
        slug = _read_json(self.root / "_active.json", {}).get("campagne")
        found = self.list()
        for c in found:
            if c.slug == slug:
                return c
        return max(found, key=lambda c: c.meta.get("creee_le", ""), default=None)

    def set_active(self, campaign: Campaign) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _write_json(self.root / "_active.json", {"campagne": campaign.slug})

    def create(self, titre: str, jeu: str, mj: dict, players: list) -> Campaign:
        """Crée la campagne (dossiers, joueurs.json, modèle de contexte.md) et l'active."""
        slug = slugify(titre)
        if not slug:
            raise CampaignError("Nom de campagne invalide.")
        path = self.root / slug
        if (path / "campagne.json").exists():
            raise CampaignError(f"La campagne « {titre} » existe déjà.")
        path.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (path / sub).mkdir(exist_ok=True)
        _write_json(path / "campagne.json", {"titre": titre.strip(), "jeu": jeu.strip(),
                                             "creee_le": datetime.now().isoformat(timespec="seconds")})
        campaign = Campaign(path)
        joueurs = build_joueurs(mj, players)
        campaign.save_joueurs(joueurs)
        campaign.contexte_path.write_text(contexte_template(campaign.titre, campaign.jeu, joueurs), encoding="utf-8")
        self.set_active(campaign)
        return campaign


def _person(member: dict) -> dict:
    pseudos = []
    for key in ("name", "username"):
        value = str(member.get(key) or "").strip()
        if value and value.lower() not in [p.lower() for p in pseudos]:
            pseudos.append(value)
    return {"discord_id": str(member.get("id", "")), "pseudos": pseudos}


def build_joueurs(mj: dict | None, players: list) -> dict:
    """joueurs.json initial : MJ (celui qui crée) et joueurs présents. Les personnages restent à renseigner."""
    boss = _person(mj) if mj else {"discord_id": "", "pseudos": []}      # sans MJ désigné : à définir avec /campagne mj
    return {
        "_note": "Les pseudos Discord changent, les identifiants non. 'personnage' est à renseigner (ou proposé par /campagne contexte).",
        "mj": {"nom": "MJ", **boss},
        "joueurs": [{"joueur": (p.get("name") or p.get("username") or str(p.get("id"))), "personnage": "", **_person(p)}
                    for p in players if str(p.get("id")) != boss["discord_id"]],
    }


def set_voix(campaign: Campaign, name: str) -> None:
    """Enregistre la voix par défaut de la campagne dans campagne.json (« » = retour à la règle générale)."""
    meta = _read_json(campaign.path / "campagne.json", {})
    if name:
        meta["voix"] = name
    else:
        meta.pop("voix", None)
    _write_json(campaign.path / "campagne.json", meta)
    campaign.meta = meta


def set_mj(campaign: Campaign, member: dict) -> None:
    """Désigne le MJ (il sort de la liste des joueurs s'il y figurait)."""
    data = campaign.joueurs()
    boss = _person(member)
    data["joueurs"] = [j for j in data["joueurs"] if j.get("discord_id") != boss["discord_id"]]
    data["mj"] = {"nom": "MJ", **boss}
    campaign.save_joueurs(data)


def contexte_personnages(campaign: Campaign) -> list:
    """Noms des personnages décrits dans contexte.md (titres « ### Nom — joué par X »), hors modèle non rempli."""
    try:
        text = campaign.contexte_path.read_text(encoding="utf-8")
    except OSError:
        return []
    names = []
    for m in re.finditer(r"^###\s+(.+?)\s*$", text, re.M):
        name = re.split(r"\s+(?:—|–|-)\s+", m.group(1))[0].strip()
        if name and not name.lower().startswith("personnage à nommer"):
            names.append(name)
    return names


def match_personnage(campaign: Campaign, wanted: str) -> str | None:
    """Nom exact du contexte correspondant à `wanted` (exact, début ou mot, sans casse ni accents), s'il est unique."""
    norm = lambda t: slugify(t).replace("-", " ")
    w, names = norm(wanted), contexte_personnages(campaign)
    for test in (lambda n: norm(n) == w, lambda n: norm(n).startswith(w), lambda n: w in norm(n).split()):
        hits = [n for n in names if test(n)]
        if len(hits) == 1:
            return hits[0]
    return None


def add_player(campaign: Campaign, member: dict, personnage: str = "") -> tuple:
    """Ajoute (ou met à jour) un joueur. -> (message d'état, avertissements). Le MJ ne peut pas être joueur.

    `personnage` est rapproché des personnages du contexte ; sinon il est enregistré tel quel avec un avertissement."""
    data, person = campaign.joueurs(), _person(member)
    if person["discord_id"] and person["discord_id"] == data["mj"].get("discord_id"):
        raise CampaignError("Cette personne est le MJ de la campagne.")
    warnings, perso = [], personnage.strip()
    if perso:
        found = match_personnage(campaign, perso)
        if found:
            perso = found
        elif contexte_personnages(campaign):
            warnings.append(f"« {perso} » n'est pas décrit dans contexte.md (enregistré tel quel).")
        taken = [j["joueur"] for j in data["joueurs"]
                 if (j.get("personnage") or "").lower() == perso.lower() and j.get("discord_id") != person["discord_id"]]
        if taken:
            warnings.append(f"Ce personnage est déjà joué par {', '.join(taken)}.")
    existing = next((j for j in data["joueurs"] if person["discord_id"] and j.get("discord_id") == person["discord_id"]), None)
    if existing:
        existing["pseudos"] += [p for p in person["pseudos"] if p.lower() not in [x.lower() for x in existing["pseudos"]]]
        if perso:
            existing["personnage"] = perso
        status = "mis à jour"
    else:
        data["joueurs"].append({"joueur": member.get("name") or member.get("username") or str(member.get("id")),
                                "personnage": perso, **person})
        status = "ajouté"
    campaign.save_joueurs(data)
    return status, warnings


# --- rubriques du résumé global ------------------------------------------------------------------

RE_HEADINGS = (re.compile(r"^#{1,4}\s+(.+?)\s*#*\s*$"),                 # ## Titre
               re.compile(r"^\*\*(.{1,80}?)\*\*\s*:?\s*$"),                # **Titre** seul sur la ligne
               re.compile(r"^-{3,}\s*(.+?)\s*-{3,}\s*$"))                  # ---- TITRE ----


def split_sections(text: str) -> list:
    """-> [(titre, texte)] : découpe un document par ses titres (Markdown, ligne en gras seule, ou ---- TITRE ----)."""
    sections, title, body = [], None, []

    def close():
        if title is not None:
            sections.append((title, "\n".join(body).strip()))
    for line in text.splitlines():
        stripped = line.strip()
        found = next((m.group(1).strip(" *:") for rx in RE_HEADINGS if (m := rx.match(stripped))), None)
        if found:
            close()
            title, body = found, []
        elif title is not None:
            body.append(line)
    close()
    return sections


def find_section(text: str, wanted: str) -> tuple:
    """Rubrique `wanted` (nom exact ou début / mot du titre, sans casse ni accents). -> (titre, texte)."""
    norm = lambda t: slugify(t).replace("-", " ")
    found, w = split_sections(text), norm(wanted)
    for test in (lambda t: norm(t) == w, lambda t: norm(t).startswith(w), lambda t: w in norm(t)):
        hits = [sec for sec in found if test(sec[0])]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise CampaignError(f"« {wanted} » est ambigu : " + ", ".join(h[0] for h in hits) + ".")
    raise CampaignError(f"Rubrique « {wanted} » introuvable. Rubriques : " + (", ".join(t for t, _ in found) or "aucune") + ".")


# --- résumé global versionné ---------------------------------------------------------------------

def versions(campaign: Campaign) -> list:
    d = campaign.path / "resume_global"
    return sorted(d.glob("v[0-9][0-9][0-9]_*.md")) if d.is_dir() else []


def add_version(campaign: Campaign, text: str, label: str) -> Path:
    """Nouvelle version du résumé global (vNNN_label.md) et mise à jour de resume_global.md."""
    d = campaign.path / "resume_global"
    d.mkdir(exist_ok=True)
    last = versions(campaign)
    n = int(last[-1].name[1:4]) + 1 if last else 1
    path = d / f"v{n:03d}_{slugify(label) or 'maj'}.md"
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    (campaign.path / "resume_global.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def rollback(campaign: Campaign) -> tuple:
    """Annule la dernière version du résumé global (archivée dans resume_global/annulees/, jamais supprimée).
    La séance qui a produit cette version est déplacée dans seances/_annulees/.
    -> (version annulée, version désormais active, identifiant de la séance annulée ou None)."""
    found = versions(campaign)
    if len(found) < 2:
        raise CampaignError("Rien à annuler : il n'y a pas de version antérieure du résumé global.")
    undone, previous = found[-1], found[-2]
    trash = campaign.path / "resume_global" / "annulees"
    trash.mkdir(exist_ok=True)
    target = trash / undone.name
    k = 1
    while target.exists():
        k += 1
        target = trash / f"{undone.stem}_{k}{undone.suffix}"
    undone.rename(target)
    (campaign.path / "resume_global.md").write_text(previous.read_text(encoding="utf-8"), encoding="utf-8")
    session = None
    seances = campaign.path / "seances"
    for meta in (sorted(seances.glob("*/meta.json")) if seances.is_dir() else []):
        if _read_json(meta, {}).get("version") == undone.name:
            session, src = meta.parent.name, meta.parent
            gone = seances / "_annulees"
            gone.mkdir(exist_ok=True)
            dest, k = gone / session, 1
            while dest.exists():
                k += 1
                dest = gone / f"{session}_{k}"
            src.rename(dest)
            break
    return undone.name, previous.name, session


def import_journal(campaign: Campaign, text: str) -> Path:
    """Initialise le résumé global avec le journal existant des joueurs (version 1)."""
    if versions(campaign):
        raise CampaignError("Cette campagne a déjà un résumé global : utilise `rollback` pour revenir en arrière.")
    if not text.strip():
        raise CampaignError("Le journal est vide.")
    return add_version(campaign, text, "journal initial")


def contexte_template(titre: str, jeu: str, joueurs: dict) -> str:
    lines = [TEMPLATE_MARK, f"# Campagne : {titre} ({jeu})", "",
             f"Maître de jeu : {', '.join(joueurs['mj']['pseudos']) or 'à renseigner'}.", "",
             "Visibilité des faits (à noter après chaque fait) : **[groupe]** connu de tous les joueurs · "
             "**[secret:Nom]** ne doit pas être révélé aux autres joueurs dans un texte partagé · **[mj]** réservé au MJ.",
             "", "## Personnages joueurs", ""]
    for j in joueurs["joueurs"]:
        lines += [f"### Personnage à nommer — joué par {j['joueur']}", "- Description, traits, classe [groupe]",
                  "- Secret éventuel [secret:Nom]", ""]
    lines += ["## Lieux, factions et PNJ importants", "- ...", "",
              "## Variantes de transcription à corriger (Whisper)",
              "- Nom correct : variantes entendues (sert aussi de glossaire pour la reconnaissance vocale)", ""]
    return "\n".join(lines)


PROMPT_CONTEXTE = """Tu aides un maître de jeu à préparer le fichier de contexte de sa campagne de jeu de rôle sur table ({jeu}).
Il te donne un texte libre décrivant ses personnages et ses joueurs. Tu dois le restructurer, sans rien inventer.

Règles :
1. FIDÉLITÉ : n'ajoute aucun fait absent du texte. Reformule seulement pour structurer. Conserve les détails (traits, secrets, histoire).
2. VISIBILITÉ : après chaque fait, note [groupe] s'il est clairement connu de tous les joueurs ; [secret:NomDuPersonnage] si le texte dit qu'il est secret, que personne ne doit le savoir, ou que le personnage l'ignore ; [mj] s'il est réservé au MJ ; et [à confirmer] dans tous les autres cas. En cas de doute, [à confirmer]. N'utilise jamais [groupe] par défaut.
3. PSEUDOS : les joueurs sont connus sous ces pseudos Discord : {pseudos}. Associe chaque personnage au joueur qui l'interprète d'après le texte.
4. GLOSSAIRE : liste des noms propres du texte ayant une orthographe qu'une reconnaissance vocale pourrait déformer, avec des variantes plausibles, sous le titre « Variantes de transcription à corriger (Whisper) ». Marque ces variantes comme suggestions.
5. Langue : français.

Réponds exactement sous cette forme, sans rien avant ni après :
=== CONTEXTE ===
(le fichier au format Markdown : titre « # Campagne : {titre} », MJ, légende de visibilité, « ## Personnages joueurs » avec un « ### Nom — joué par pseudo » par personnage, « ## Lieux, factions et PNJ importants » si le texte en parle, « ## Variantes de transcription à corriger (Whisper) »)
=== JOUEURS ===
(un tableau JSON, un élément par personnage joueur : [{{"pseudo": "...", "personnage": "..."}}])
"""


def parse_contexte_response(text: str) -> tuple:
    """-> (markdown, liste [{'pseudo','personnage'}]). Lève CampaignError si le format n'est pas respecté."""
    m = re.search(r"=== CONTEXTE ===\s*(.*?)\s*=== JOUEURS ===\s*(.*)\Z", text, re.S)
    if not m:
        raise CampaignError("Réponse de Gemini inexploitable (format inattendu) : réessaie.")
    markdown, raw = m.group(1).strip(), m.group(2).strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        mapping = [e for e in json.loads(raw) if isinstance(e, dict)]
    except (ValueError, TypeError):
        mapping = []                        # le contexte reste utilisable sans la correspondance
    return markdown, mapping


def apply_mapping(joueurs: dict, mapping: list) -> int:
    """Renseigne `personnage` des joueurs d'après les pseudos ; n'écrase jamais une valeur existante. -> nombre de lignes remplies."""
    filled = 0
    for entry in mapping:
        pseudo = str(entry.get("pseudo", "")).strip().lower()
        perso = str(entry.get("personnage", "")).strip()
        if not pseudo or not perso:
            continue
        for j in joueurs["joueurs"]:
            if not j.get("personnage") and pseudo in [p.lower() for p in j["pseudos"]] + [j["joueur"].lower()]:
                j["personnage"] = perso
                filled += 1
                break
    return filled


def generate_contexte(campaign: Campaign, brut: str) -> dict:
    """Appel bloquant à Gemini (à lancer dans un thread). Écrit contexte.md (ou contexte_propose.md si le MJ a déjà
    rempli le sien) et complète les personnages de joueurs.json. -> {'fichier', 'remplis', 'total'}."""
    joueurs = campaign.joueurs()
    pseudos = ", ".join(sorted({p for j in joueurs["joueurs"] for p in j["pseudos"]})) or "(inconnus)"
    instruction = PROMPT_CONTEXTE.format(jeu=campaign.jeu or "jeu non précisé", pseudos=pseudos, titre=campaign.titre)
    answer = gemini.generate(instruction, brut, temperature=0.2, web_search=False)
    markdown, mapping = parse_contexte_response(answer)
    target = campaign.contexte_path if not campaign.contexte_rempli() else campaign.path / "contexte_propose.md"
    target.write_text(markdown + "\n", encoding="utf-8")
    filled = apply_mapping(joueurs, mapping)
    if filled:
        campaign.save_joueurs(joueurs)
    return {"fichier": target.name, "remplis": filled, "total": len(joueurs["joueurs"])}


__all__ = ["Campaign", "CampaignError", "CampaignStore", "GeminiError", "slugify", "build_joueurs",
           "contexte_template", "set_mj", "split_sections", "find_section", "add_player", "contexte_personnages", "match_personnage", "versions", "add_version", "rollback", "import_journal", "parse_contexte_response", "apply_mapping", "generate_contexte"]
