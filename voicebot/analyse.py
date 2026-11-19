"""Analyse d'une séance en trois appels à Claude : nettoyage, analyse (brouillon + questions), version finale.

1. NETTOYAGE : Claude lit le transcript (lignes numérotées) et rend une LISTE de corrections (pas un texte réécrit). Le programme
   valide chacune (la ligne existe, le texte à remplacer y figure vraiment, l'ajout n'est pas démesuré), les applique, et
   enregistre `transcript_corrige.txt` (l'original n'est jamais modifié) avec un bilan de qualité.
2. ANALYSE : sur le transcript corrigé, Claude produit les notes, le brouillon des livrables et des QUESTIONS de scénario
   (ambiguïtés, non-dits : jamais des questions sur la qualité du transcript).
3. FINAL : brouillon + réponses des joueurs + extraits du transcript autour des moments cités -> livrables définitifs, intégrés.

Chaque appel est mémorisé dans `seances/<séance>/analyse/` (clé = empreinte du prompt) : relancer après un échec ne refait pas
les appels déjà réussis. Sans question (réglage ou rien d'ambigu), l'étape 3 est inutile : 2 appels, voire 1 sans nettoyage.
Module indépendant de Discord ; les appels sont bloquants (à lancer dans un thread).
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import claude_cli, glossaire, pack
from .campagne import Campaign, CampaignError
from .config import SET
from .memoire import REGLES_COMMUNES, clean_rows, contexte_text, etat_text, hms, labeler, render_lines, session_date

DOSSIER = "analyse"
MAX_PART_AJOUT = 3                 # un remplacement ne peut pas être plus de 3 fois plus long que le texte remplacé...
MIN_MOTS_AJOUT = 12                # ... sauf s'il reste court
ALERTE_LIGNES = 0.25               # au-delà, le nettoyage est suspect
FENETRE_EXTRAIT_S = 60
EXTRAIT_MAX_LIGNES = 80
RIEN = "∅"
NON_PRECISE = "non précisé"
JE_NE_SAIS_PAS = "Je ne sais pas / laisser ambigu"

TACHE_NETTOYAGE = """TÂCHE : nettoyer et désambiguïser la transcription avant son analyse. Appuie-toi sur le contexte, l'état de la campagne, tes connaissances du jeu (Pathfinder 1re édition) et de la campagne, et sur la cohérence entre les passages.
Les lignes sont numérotées (L0001…). Ne réécris PAS la transcription : donne seulement la LISTE des corrections à appliquer, une par ligne, au format exact :
`L0042 :: texte exact à remplacer => texte corrigé :: raison très courte :: ●`
• Le texte à remplacer doit figurer MOT POUR MOT dans la ligne indiquée (reprends-en le moins possible, mais assez pour être unique).
• Corrige : noms de personnages, de lieux, de monstres, de sorts, de règles et de termes du jeu déformés par la reconnaissance vocale ; mots inventés ; homophones qui changent le sens ; qui parle de qui quand c'est évident. Écris `∅` comme texte corrigé pour supprimer un bruit ou une répétition parasite.
• Confiance : ● sûr, ◐ probable. N'écris PAS les corrections incertaines : mieux vaut laisser le texte tel quel que d'inventer.
• Ne supprime ni ne modifie jamais une demande orale de retenir ou de noter une information (« retiens bien… », « notez que… »), ni les phrases qui la précèdent ou la suivent : elles seront exploitées dans l'étape suivante.
• N'ajoute rien qui n'ait pas été dit, ne reformule pas, ne corrige pas la grammaire ni le style oral.
Format de réponse, trois sections, chacune introduite par sa ligne exacte :
=== CORRECTIONS ===
(une correction par ligne, ou « aucune »)
=== GLOSSAIRE ===
(les noms et termes que tu as corrigés plusieurs fois ou qui reviendront aux prochaines séances, une ligne chacun : `Forme correcte : variante entendue 1, variante entendue 2` ; seulement des variantes réellement présentes dans la transcription)
=== BILAN ===
(5 à 10 lignes : qualité globale de la transcription (bonne / moyenne / mauvaise) et pourquoi ; les passages les moins fiables avec leur horodatage ; ce qui reste douteux après tes corrections)"""

TACHE_FINALE = """TÂCHE FINALE : tu as rédigé un brouillon de documents à partir de la transcription. Les joueurs ont répondu à tes questions d'ambiguïté (balise <reponses>), et des extraits de la transcription autour des moments concernés sont fournis (balise <extraits>).
Produis la VERSION DÉFINITIVE des documents :
• intègre chaque réponse là où elle lève une ambiguïté (compte rendu, chronologie, sections, « précédemment ») et retire de « Réserves » les points résolus ;
• une réponse « non précisé » ou « Je ne sais pas » signifie que l'ambiguïté reste : garde-la dans « Réserves » sans trancher ;
• une réponse libre des joueurs fait foi sur ta déduction ; si elle contredit clairement les extraits, signale-le dans « Réserves » ;
• le reste du brouillon est conservé, sans nouvelle invention. Pas de notes, pas de ligne FIN LUE, pas de questions."""


class AnalyseError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


@dataclass
class Question:
    id: str
    texte: str
    pourquoi: str = ""
    reperes: list = field(default_factory=list)        # secondes
    choix: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "texte": self.texte, "pourquoi": self.pourquoi, "reperes": self.reperes, "choix": self.choix}

    @staticmethod
    def from_dict(d: dict) -> "Question":
        return Question(d["id"], d["texte"], d.get("pourquoi", ""), list(d.get("reperes", [])), list(d.get("choix", [])))


# --- étape 1 : corrections ----------------------------------------------------------------------

def numeroter(rows: list) -> str:
    """Lignes `L0001 [hh:mm:ss] Qui: texte` (numéro = position dans la liste)."""
    return "\n".join(f"L{i:04d} [{hms(s)}] {who}: {t}" for i, (s, _e, who, t) in enumerate(rows, 1))


def parse_corrections(block: str) -> list:
    """-> [{'ligne': int, 'ancien': str, 'nouveau': str, 'raison': str, 'confiance': '●|◐|○'}] (lignes illisibles ignorées)."""
    out = []
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-•*`").strip().rstrip("`")
        parts = [p.strip() for p in line.split("::")]
        m = re.fullmatch(r"L?(\d{1,6})", parts[0]) if parts else None
        if not m or len(parts) < 2:
            continue
        change = re.split(r"\s*(?:=>|→|->)\s*", parts[1], maxsplit=1)
        if len(change) != 2 or not change[0].strip():
            continue
        tail = " ".join(parts[2:])
        conf = next((c for c in "●◐○" if c in tail), "◐")
        reason = tail.replace("●", "").replace("◐", "").replace("○", "").strip(" :")
        out.append({"ligne": int(m.group(1)), "ancien": change[0].strip().strip("«»\"'` "),
                    "nouveau": change[1].strip().strip("«»\"'` "), "raison": reason, "confiance": conf})
    return out


def _words(text: str) -> int:
    return len(text.split())


def apply_corrections(rows: list, corrections: list) -> tuple:
    """-> (nouvelles_lignes, appliquees, rejetees, avertissements). `rows` n'est pas modifié ; chaque rejet porte 'motif'."""
    new, applied, rejected, changed = [list(r) for r in rows], [], [], set()
    for c in corrections:
        i = c["ligne"] - 1
        why = None
        if not 0 <= i < len(new):
            why = "ligne inexistante"
        elif c["confiance"] == "○":
            why = "confiance trop faible"
        elif c["ancien"] == c["nouveau"]:
            why = "aucun changement"
        elif c["nouveau"] != RIEN and _words(c["nouveau"]) > MIN_MOTS_AJOUT and len(c["nouveau"]) > MAX_PART_AJOUT * len(c["ancien"]):
            why = "ajout démesuré"
        if not why:
            text, repl = new[i][3], "" if c["nouveau"] == RIEN else c["nouveau"]
            if c["ancien"] in text:
                updated = text.replace(c["ancien"], repl, 1)
            else:
                found = re.search(re.escape(c["ancien"]), text, re.I)
                updated = (text[:found.start()] + repl + text[found.end():]) if found else None
            if updated is None:
                why = "texte à remplacer introuvable dans la ligne"
            else:
                new[i][3] = re.sub(r"\s{2,}", " ", updated).strip()
                changed.add(i)
                applied.append(c)
        if why:
            rejected.append({**c, "motif": why})
    kept = [r for r in new if r[3].strip(" .,;!?…-")]
    warns = []
    if rows and len(changed) / len(rows) > ALERTE_LIGNES:
        warns.append(f"Le nettoyage a modifié {len(changed)} lignes sur {len(rows)} ({len(changed) * 100 // len(rows)} %) : "
                     "beaucoup plus que d'habitude, compare `transcript_corrige.txt` à l'original.")
    return kept, applied, rejected, warns


def split_reponse(text: str, names: list) -> dict:
    return {n: pack._section(text.replace("\r\n", "\n"), n, names) for n in names}


# --- étape 2 : questions ------------------------------------------------------------------------

def parse_questions(block: str, maximum: int) -> list:
    out = []
    for part in re.split(r"^[#*\s-]*QUESTION\s*\d*\s*[-#*\s]*$", block or "", flags=re.M | re.I)[1:]:
        fields = {}
        for key in ("Question", "Pourquoi", "Repères", "Choix"):
            m = re.search(rf"^[\s*-]*{key}\s*:\s*(.*?)(?=^[\s*-]*(?:Question|Pourquoi|Repères|Choix)\s*:|\Z)", part, re.S | re.M | re.I)
            fields[key] = m.group(1).strip() if m else ""
        if not fields["Question"]:
            continue
        marks = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in re.findall(r"(\d+):(\d\d):(\d\d)", fields["Repères"])]
        choices = [c.strip(" *`\"«»") for c in re.split(r"\s*\|\s*|\n", fields["Choix"]) if c.strip(" *`\"«»-")]
        out.append(Question(f"q{len(out) + 1}", fields["Question"], fields["Pourquoi"], marks[:4], choices[:4]))
        if len(out) >= maximum:
            break
    return out


def extraits(rows: list, questions: list, reponses: dict) -> str:
    """Lignes du transcript corrigé autour des repères des questions (±60 s), regroupées par question."""
    blocks = []
    for q in questions:
        picked = sorted({i for t in q.reperes for i, r in enumerate(rows) if abs(r[0] - t) <= FENETRE_EXTRAIT_S})
        if not picked:
            continue
        lines = render_lines([rows[i] for i in picked[:EXTRAIT_MAX_LIGNES]])
        blocks.append(f"[{q.id}] {q.texte}\n{lines}")
    return "\n\n".join(blocks)


def reponses_texte(questions: list, reponses: dict) -> str:
    out = []
    for q in questions:
        r = reponses.get(q.id) or {}
        answer = (r.get("reponse") or "").strip()
        if not answer or answer == JE_NE_SAIS_PAS:
            answer = NON_PRECISE + (" : les joueurs ne savent pas" if r.get("reponse") else "")
        out.append(f"[{q.id}] Question : {q.texte}\n    Réponse : {answer}" + (f" (par {r['par']})" if r.get("par") and answer[:11] != NON_PRECISE else ""))
    return "\n".join(out)


# --- orchestration ------------------------------------------------------------------------------

class Analyse:
    def __init__(self, campaign: Campaign, rows: list, session_id: str, nettoyage: bool | None = None,
                 questions: bool | None = None, progress=None):
        self.campaign, self.session_id, self.rows = campaign, session_id, rows
        self.nettoyage = SET["claude.nettoyage"] if nettoyage is None else nettoyage
        self.questions_on = SET["claude.questions"] if questions is None else questions
        self.say = progress or (lambda _t: None)
        self.seance = campaign.path / "seances" / session_id
        self.dir = self.seance / DOSSIER
        self.appels: list = []                 # statistiques de chaque appel
        self.avertissements: list = []
        self.cleaned = clean_rows(rows, labeler(campaign.joueurs()), SET["campagnes.pack_fusion_s"])
        if not self.cleaned:
            raise CampaignError("Aucune parole exploitable dans cette séance.")
        self.corrige = self.cleaned
        self.glossaire_propose: list = []
        self.brouillon = ""
        self.notes = ""
        self.questions: list = []

    # appel mémorisé -----------------------------------------------------------------------------
    def _ask(self, name: str, prompt: str):
        self.dir.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        f, marker = self.dir / f"{name}.md", self.dir / f"{name}.sha"
        if f.exists() and marker.exists() and marker.read_text(encoding="utf-8").strip() == sha:
            self.appels.append({"etape": name, "reprise": True})
            return f.read_text(encoding="utf-8")
        try:
            rep = claude_cli.ask(prompt)
        except claude_cli.ClaudeError as e:
            raise AnalyseError(str(e)) from e
        f.write_text(rep.texte + "\n", encoding="utf-8")
        marker.write_text(sha + "\n", encoding="utf-8")
        self.appels.append({"etape": name, "modele": rep.modele, "duree_s": round(rep.duree_s, 1), "tokens_entree": rep.tokens_entree,
                            "tokens_sortie": rep.tokens_sortie, "cout_usd_estime": rep.cout_usd})
        return rep.texte

    def _entete(self, with_state: bool = True) -> list:
        date = session_date(self.session_id)
        blocks = [REGLES_COMMUNES.strip(), pack._tag("contexte", contexte_text(self.campaign, self.session_id))]
        if with_state:
            blocks.append(pack._tag("etat_campagne", etat_text(self.campaign)))
        return blocks + [f"Séance du {date}."]

    # 1 ------------------------------------------------------------------------------------------
    def nettoyer(self) -> dict:
        """Étape 1. -> {'appliquees', 'rejetees', 'bilan', 'avertissements'} ; `self.corrige` contient le transcript corrigé."""
        if not self.nettoyage:
            return {"appliquees": [], "rejetees": [], "bilan": "", "avertissements": []}
        self.say("Claude nettoie la transcription")
        prompt = "\n\n".join(self._entete() + [pack._tag("transcription", numeroter(self.cleaned)), TACHE_NETTOYAGE])
        answer = self._ask("1_nettoyage", prompt)
        parts = split_reponse(answer, ["CORRECTIONS", "GLOSSAIRE", "BILAN"])
        warns = []
        if parts["CORRECTIONS"] is None:
            warns.append("Le nettoyage n'a pas renvoyé de section `=== CORRECTIONS ===` : aucune correction appliquée.")
        corrections = parse_corrections(parts["CORRECTIONS"] or "")
        self.corrige, applied, rejected, more = apply_corrections(self.cleaned, corrections)
        warns += more
        self.glossaire_propose = glossaire.parse_termes(parts["GLOSSAIRE"] or "")
        bilan = (parts["BILAN"] or "").strip()
        self._ecrire_nettoyage(applied, rejected, bilan)
        self.avertissements += warns
        return {"appliquees": applied, "rejetees": rejected, "bilan": bilan, "avertissements": warns}

    def _ecrire_nettoyage(self, applied: list, rejected: list, bilan: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "transcript_corrige.txt").write_text(
            f"# Transcript corrigé par Claude : séance {self.session_id} ({len(applied)} corrections)\n\n" + render_lines(self.corrige) + "\n",
            encoding="utf-8")
        (self.dir / "bilan.md").write_text("# Bilan de qualité du transcript\n\n" + (bilan or "(aucun bilan)") + "\n", encoding="utf-8")
        lines = [f"# Corrections ({len(applied)} appliquées, {len(rejected)} rejetées)", "", "## Appliquées"]
        lines += [f"- L{c['ligne']:04d} {c['confiance']} « {c['ancien']} » → « {c['nouveau']} » ({c['raison']})" for c in applied]
        lines += ["", "## Rejetées"] + [f"- L{c['ligne']:04d} « {c['ancien']} » → « {c['nouveau']} » : {c['motif']}" for c in rejected]
        (self.dir / "corrections.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.dir / "corrections.json").write_text(json.dumps({"appliquees": applied, "rejetees": rejected}, ensure_ascii=False, indent=1) + "\n",
                                                   encoding="utf-8")

    # 2 ------------------------------------------------------------------------------------------
    def analyser(self) -> list:
        """Étape 2. -> liste de Question (vide : rien à demander). Remplit `self.brouillon` et `self.notes`."""
        self.say("Claude analyse la séance")
        maximum = int(SET["claude.questions_max"]) if self.questions_on else 0
        made = pack.build_packs(self.campaign, [], self.session_id, 0, cleaned=self.corrige, questions_max=maximum, pack_dir=f"{DOSSIER}/pack")
        answer = self._ask("2_analyse", made["fichiers"][0].read_text(encoding="utf-8"))
        try:
            data = pack.parse_reponse(answer)
        except CampaignError:
            (self.dir / "reponse_echec.md").write_text(answer + "\n", encoding="utf-8")
            raise
        self.avertissements += pack.controle_lecture(data, made["parties"][-1])
        self.notes, self.reponse2 = data["notes"], answer
        self.brouillon = re.split(r"^[#*\s]*===\s*QUESTIONS\s*===", answer.replace("\r\n", "\n"), flags=re.M | re.I)[0].strip()
        self.questions = parse_questions(data["questions"], maximum) if maximum else []
        (self.dir / "questions.json").write_text(json.dumps([q.to_dict() for q in self.questions], ensure_ascii=False, indent=1) + "\n",
                                                 encoding="utf-8")
        return self.questions

    def reponses_sauvees(self) -> dict | None:
        """Réponses déjà recueillies pour ces mêmes questions (reprise après un échec de l'étape 3)."""
        try:
            data = json.loads((self.dir / "reponses.json").read_text(encoding="utf-8"))
            same = data["questions"] == [q.to_dict() for q in self.questions]
            return data["reponses"] if same else None
        except (OSError, ValueError, KeyError):
            return None

    # 3 ------------------------------------------------------------------------------------------
    def finaliser(self, reponses: dict | None = None) -> dict:
        """Étape 3 (ou intégration directe du brouillon sans question). -> résultat de `pack.integrate` + 'glossaire' (lignes ajoutées)."""
        if self.questions:
            reponses = reponses or {}
            (self.dir / "reponses.json").write_text(json.dumps({"questions": [q.to_dict() for q in self.questions], "reponses": reponses},
                                                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            self.say("Claude rédige la version finale")
            date = session_date(self.session_id)
            livrables = pack.TACHE_LIVRABLES.format(
                mots=pack.longueur_precedemment(), date=date, campagne=self.campaign.titre, structure=pack.STRUCTURE_ETAT,
                rubriques=", ".join(f"« {t} »" for t in pack.fusion.titles(etat_text(self.campaign))) or "(aucune)")
            prompt = "\n\n".join(self._entete() + [
                pack._tag("brouillon", self.brouillon), pack._tag("reponses", reponses_texte(self.questions, reponses)),
                pack._tag("extraits", extraits(self.corrige, self.questions, reponses) or "(aucun extrait)"),
                TACHE_FINALE, livrables, pack.RETENIR_FINAL, "Réponds avec les documents seulement, chacun introduit par sa ligne exacte `=== COMPTE RENDU ===`, "
                "`=== CHANGEMENTS ===`, `=== CHRONOLOGIE ===`, `=== SECTIONS ===`, `=== PRECEDEMMENT ===`."])
            final = self._ask("3_final", prompt)
        else:
            final = self.reponse2
        extra = self.meta()
        try:
            out = pack.integrate(self.campaign, final, self.session_id, source="claude", extra=extra, controle=False, extractions=self.notes,
                                 avertissements=self.avertissements)
        except CampaignError:
            (self.dir / "reponse_echec.md").write_text(final + "\n", encoding="utf-8")
            raise
        out["glossaire"] = self._maj_glossaire(reponses or {})
        out["appels"] = self.appels
        out["meta"] = extra
        return out

    def meta(self) -> dict:
        real = [a for a in self.appels if not a.get("reprise")]
        cost = [a["cout_usd_estime"] for a in real if a.get("cout_usd_estime") is not None]
        return {"moteur": "claude", "modele": SET["claude.modele"], "effort": SET["claude.effort"], "appels": len(real),
                "appels_repris": len(self.appels) - len(real), "nettoyage": self.nettoyage, "questions": len(self.questions),
                "duree_s": round(sum(a.get("duree_s", 0) for a in real), 1), "tokens_entree": sum(a.get("tokens_entree", 0) for a in real),
                "tokens_sortie": sum(a.get("tokens_sortie", 0) for a in real), "cout_usd_estime": round(sum(cost), 4) if cost else None}

    def _maj_glossaire(self, reponses: dict) -> list:
        """Ajoute au glossaire les termes corrigés (variantes réellement présentes) et les réponses des joueurs ; mémorise l'ajout."""
        original = render_lines(self.cleaned)
        terms = glossaire.filtrer_termes(self.glossaire_propose, original, glossaire.texte(self.campaign.path))
        date, decisions = session_date(self.session_id), []
        for q in self.questions:
            r = reponses.get(q.id) or {}
            if r.get("reponse") and r["reponse"] != JE_NE_SAIS_PAS:
                line = f"{date} · {r.get('par') or 'joueur'} · {q.texte} → {r['reponse']}"
                decisions.append(" ".join(line.split()) + " " + glossaire.etiquette(self.session_id))
        glossaire.purger(self.campaign.path, self.session_id)             # jamais deux fois les décisions d'une même séance
        added = glossaire.ajouter(self.campaign.path, terms, decisions)
        if added:
            (self.dir / glossaire.AJOUTS).write_text("\n".join(added) + "\n", encoding="utf-8")
        return added


def run_sans_question(campaign: Campaign, rows: list, session_id: str, progress=None, **kw) -> dict:
    """Enchaîne les étapes 1 et 2 puis l'intégration quand il n'y a rien à demander (utile aux tests et au mode sans interaction)."""
    a = Analyse(campaign, rows, session_id, progress=progress, **kw)
    a.nettoyer()
    a.analyser()
    return a.finaliser({})
