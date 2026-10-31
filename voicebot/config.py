"""Configuration : tous les paramètres vivent dans config.ini (lecture, écriture en place, valeurs par défaut)."""
import configparser
import json
import os
import re
import sys
import threading
from pathlib import Path


# ------------------------------------------------------------------ configuration (config.ini)
#
# Tous les paramètres vivent dans un fichier INI séparé (config.ini par défaut ; autre emplacement avec
# --config chemin ou la variable BOT_CONFIG). Il est créé avec les valeurs par défaut au premier lancement,
# et les paramètres manquants y sont ajoutés automatiquement après une mise à jour du programme.
# Priorité : variable d'environnement historique (ex. WHISPER_MODEL) > config.ini > valeur par défaut.

DEFAULT_HALLUCINATIONS = r"sous-?titr|amara\.org|merci d['’]avoir regard|abonne[zr]-vous"

SECTION_TITLES = {
    "discord": "Discord",
    "sessions": "Enregistrements",
    "audio": "Archivage audio",
    "vad": "Détection de voix (VAD) et découpage en énoncés",
    "whisper": "Transcription WhisperX",
    "live": "Suivi en direct (console, statut du bot, file d'attente)",
    "stats": "Statistiques",
    "rapport": "Rapport PDF",
    "fin_de_session": "Traitement de fin de session (modifiable avec /config pour les étapes)",
    "mp3": "Conversion MP3",
    "drive": "Google Drive",
    "regles": "Questions de règles (commande /regle, Google Gemini)",
}

# (section.clé, type, défaut, ancienne variable d'environnement, description)
SETTINGS = [
    ("discord.token", str, "", "DISCORD_TOKEN",
     "Jeton du bot. Mieux vaut le laisser vide et définir la variable d'environnement DISCORD_TOKEN."),
    ("discord.guild_id", str, "", "DISCORD_GUILD_ID",
     "Identifiant de ton serveur : les commandes apparaissent instantanément (vide = commandes globales, plus lentes)."),
    ("discord.message_consentement", str,
     "🔴 **Enregistrement en cours** dans {canal}. L'audio de chaque participant et sa transcription sont "
     "conservés. Quitte le canal si tu ne souhaites pas être enregistré. `/stop` pour terminer.", None,
     "Message affiché au /record. {canal} est remplacé par le canal vocal."),
    ("sessions.dossier", str, "sessions", None, "Dossier où sont créées les sessions."),
    ("audio.tolerance_trou_ms", int, 200, None,
     "Gigue réseau tolérée (ms) avant de considérer qu'un utilisateur s'est tu et de combler avec du silence."),
    ("audio.flush_s", float, 1.0, None, "Intervalle (s) entre deux écritures sur disque des fichiers WAV."),
    ("audio.meta_toutes_les_s", int, 10, None, "Intervalle (s) de mise à jour de meta.json pendant l'enregistrement."),
    ("vad.agressivite", int, 2, "VAD_AGGRESSIVENESS",
     "webrtcvad, de 0 (permissif) à 3 (strict). Plus strict = moins de bruit détecté comme voix."),
    ("vad.seuil_energie_rms", float, 400.0, "VAD_RMS",
     "Seuil RMS du VAD de repli (utilisé seulement si webrtcvad n'est pas installé)."),
    ("vad.pre_roll_ms", int, 300, None, "Audio conservé (ms) avant le début détecté de la parole."),
    ("vad.debut_fenetre", int, 5, None, "Début de parole : nombre de trames de 20 ms examinées..."),
    ("vad.debut_trames_voisees", int, 3, None, "... dont au moins ce nombre doivent être voisées."),
    ("vad.silence_fin_ms", int, 700, None, "Fin d'énoncé après ce silence (ms)."),
    ("vad.marge_fin_ms", int, 200, None, "Audio conservé (ms) après la dernière trame voisée."),
    ("vad.enonce_max_s", int, 20, None, "Durée maximale (s) d'un énoncé avant découpage forcé."),
    ("vad.min_trames_voisees", int, 10, None, "Un énoncé a au moins ce nombre de trames voisées (10 = 200 ms), sinon c'est du bruit."),
    ("vad.niveau_min_dbfs", float, -45.0, "MIN_UTTERANCE_DBFS",
     "Un énoncé plus faible que ce niveau RMS (dBFS) n'est pas envoyé à WhisperX."),
    ("whisper.modele", str, "small", "WHISPER_MODEL", "Modèle WhisperX : tiny, base, small, medium, large-v3..."),
    ("whisper.device", str, "", "WHISPER_DEVICE", "cuda ou cpu (vide = détection automatique)."),
    ("whisper.calcul", str, "", "WHISPER_COMPUTE", "float16, int8... (vide = float16 sur GPU, int8 sur CPU)."),
    ("whisper.langue", str, "fr", "WHISPER_LANG", "Langue parlée."),
    ("whisper.batch", int, 0, "WHISPER_BATCH", "Taille de batch (0 = automatique : 8 sur GPU, 1 sur CPU)."),
    ("whisper.alignement", bool, True, "WHISPERX_ALIGN", "Alignement mot à mot : timestamps plus précis, un peu plus lent."),
    ("whisper.filtre_hallucinations", str, DEFAULT_HALLUCINATIONS, None,
     "Expression régulière : les segments qui la contiennent sont rejetés (sous-titres inventés sur du silence)."),
    ("whisper.mots_seuls_ignores", str, "merci", None,
     "Mots (séparés par des virgules ou des espaces) : une phrase qui ne contient QUE ces mots, éventuellement répétés\n"
     "(« Merci. », « merci, merci ! »), est rejetée comme bruit. « Merci beaucoup » est conservé. Vide = rien d'ignoré."),
    ("live.debug", bool, False, "LIVE_DEBUG", "Trace chaque énoncé (durée, niveau, décision) pour régler les seuils."),
    ("live.statut_console_s", float, 10.0, "LIVE_STATUS_INTERVAL",
     "Secondes entre deux lignes de suivi en console pendant un REC (0 = seulement les changements d'état)."),
    ("live.retard_alerte_s", float, 30.0, "ASR_BACKLOG_WARN",
     "Secondes d'audio en attente qui remplissent la barre et déclenchent l'alerte."),
    ("live.historique", int, 500, None, "Nombre de messages gardés en mémoire pour /transcript."),
    ("live.statut_discord_texte_s", float, 15.0, None,
     "Délai minimal (s) entre deux changements de texte du statut Discord (limite de Discord)."),
    ("live.statut_scrutation_s", float, 1.0, None, "Fréquence (s) de vérification de l'état pour le statut et la console."),
    ("live.attente_fin_asr_s", int, 300, None, "À /stop : attente maximale (s) que WhisperX vide sa file."),
    ("stats.fenetre_interruption_s", float, 30.0, None, "Interruptions : on cherche l'intervenant coupé dans cette fenêtre (s)."),
    ("stats.chevauchement_interruption_s", float, 0.3, None, "Chevauchement minimal (s) pour compter une interruption."),
    ("stats.pause_monologue_s", float, 3.0, None, "Pause maximale (s) entre deux segments d'un même monologue."),
    ("stats.monologue_min_s", float, 8.0, None, "Durée minimale (s) pour afficher le plus long monologue."),
    ("stats.parole_min_mots_min_s", float, 10.0, None, "Temps de parole minimal (s) pour calculer les mots/min."),
    ("stats.silence_affiche_min_s", float, 10.0, None, "Durée minimale (s) pour afficher le plus long silence."),
    ("stats.mot_favori_min", int, 3, None, "Occurrences minimales pour qu'un mot soit « favori »."),
    ("stats.nb_mots_top", int, 5, None, "Nombre de mots dans « mots les plus utilisés »."),
    ("stats.lignes_tableau_discord", int, 15, None, "Intervenants affichés dans le tableau Discord."),
    ("stats.mots_vides_supplementaires", str, "", None,
     "Mots à ignorer en plus de la liste intégrée pour les stats de vocabulaire (séparés par des espaces ou virgules)."),
    ("rapport.max_intervenants_graphiques", int, 12, None, "Intervenants affichés dans les graphiques du PDF."),
    ("fin_de_session.mp3", bool, True, None, "Convertir les fichiers WAV en MP3"),
    ("fin_de_session.mp3_kbps", int, 64, None, "Débit MP3 en kbit/s (voix mono : 48 à 96 suffit)"),
    ("fin_de_session.rapport", bool, True, None, "Générer le rapport PDF (avec les statistiques)"),
    ("fin_de_session.drive", bool, True, None, "Déposer les fichiers sur Google Drive"),
    ("fin_de_session.drive_audio", bool, True, None, "Drive : inclure l'audio (MP3, ou WAV si la conversion est désactivée)"),
    ("fin_de_session.drive_transcript", bool, True, None, "Drive : inclure le transcript"),
    ("fin_de_session.drive_rapport", bool, True, None, "Drive : inclure le rapport PDF"),
    ("fin_de_session.partage", str, "lien", None,
     "Drive : « lien » = toute personne ayant le lien peut consulter, « aucun » = privé"),
    ("fin_de_session.commande_retraitement", str, "", "REPROCESS_CMD",
     "Commande lancée en fin de session, ex. python reprocess.py (reçoit le dossier de session en argument)."),
    ("mp3.ffmpeg", str, "ffmpeg", "FFMPEG", "Chemin de ffmpeg s'il n'est pas dans le PATH."),
    ("drive.identifiants", str, "credentials.json", "GOOGLE_CREDENTIALS", "Client OAuth « application de bureau »."),
    ("drive.jeton", str, "token.json", "GOOGLE_TOKEN", "Jeton Google mis en cache après la première autorisation."),
    ("drive.dossier_racine", str, "Enregistrements Discord", "DRIVE_ROOT_NAME", "Dossier racine sur le Drive."),
    ("drive.attente_connexion_s", int, 180, None, "Attente maximale (s) de la connexion Google avant échec de l'étape."),
    ("drive.taille_bloc_mo", int, 8, None, "Taille des blocs d'envoi (Mo)."),
    ("regles.activer", bool, True, None, "Active la commande /regle."),
    ("regles.cle_api", str, "", "GEMINI_API_KEY",
     "Clé d'API Google Gemini (https://aistudio.google.com/apikey). Mieux vaut la laisser vide\n"
     "et définir la variable d'environnement GEMINI_API_KEY."),
    ("regles.modele", str, "gemini-3.8-flash", None,
     "Modèle Gemini. « gemini-flash-latest » suit automatiquement la dernière version Flash."),
    ("regles.jeu_par_defaut", str, "Pathfinder première édition", None,
     "Jeu utilisé quand /regle est lancée sans préciser le jeu."),
    ("regles.nb_phrases", int, 15, None, "Nombre de dernières phrases du transcript envoyées à Gemini (1 à 50)."),
    ("regles.temperature", float, 0.2, None, "Créativité de la réponse (0 = très factuel, 2 = très libre)."),
    ("regles.reflexion", str, "low", None,
     "Niveau de « réflexion » de Gemini 3.x : low (rapide, recommandé), medium, high (plus lent, souvent plusieurs\n"
     "dizaines de secondes), minimal (pas toujours accepté), auto (ne rien envoyer : valeur par défaut du modèle,\n"
     "à utiliser avec un modèle qui ne gère pas ce réglage, comme gemini-2.5-flash)."),
    ("regles.recherche_web", bool, False, None,
     "Autorise Gemini à s'appuyer sur la recherche Google pour vérifier la règle (plus fiable, plus lent)."),
    ("regles.delai_attente_s", int, 60, None, "Temps maximal d'attente de la réponse de Gemini (s)."),
    ("regles.delai_min_s", int, 10, None, "Délai minimal (s) entre deux /regle sur un même serveur (évite les abus)."),
    ("regles.instruction", str,
     "Tu es arbitre et expert des règles du jeu « {jeu} ». Tu reçois les dernières phrases échangées à l'oral par "
     "des joueurs autour de la table (transcription automatique : erreurs, noms propres déformés, phrases coupées "
     "possibles). Repère le point de règle dont ils parlent ou sur lequel ils hésitent, puis : 1) formule-le en une "
     "phrase ; 2) explique la règle précisément, avec conditions, exceptions et cas limites utiles ; 3) tranche le cas "
     "concret évoqué s'il y en a un ; 4) cite la source (livre, chapitre) seulement si tu en es sûr. Si tu n'es pas "
     "certain, ou si plusieurs interprétations existent (errata, différences entre éditions, règle optionnelle), dis-le "
     "clairement plutôt que d'inventer. Réponds en français, de façon concise (moins de 1500 caractères), sans "
     "introduction ni formule de politesse. Si aucune question de règle ne ressort, dis-le en une phrase.", None,
     "Consigne envoyée à Gemini. {jeu} est remplacé par le nom du jeu. Doit tenir sur une seule ligne."),
]
SPEC = {s[0]: s for s in SETTINGS}

_TRUE = {"1", "true", "vrai", "oui", "on", "o", "y", "yes"}
_FALSE = {"0", "false", "faux", "non", "off", "n", "no"}
CHECKS = {   # clé : (test, message)
    "vad.agressivite": (lambda v: 0 <= v <= 3, "attendu : un entier de 0 à 3"),
    "fin_de_session.mp3_kbps": (lambda v: 32 <= v <= 320, "attendu : un débit entre 32 et 320"),
    "fin_de_session.partage": (lambda v: v in ("lien", "aucun"), "attendu : lien ou aucun"),
    "audio.flush_s": (lambda v: v > 0, "attendu : un nombre > 0"),
    "live.statut_scrutation_s": (lambda v: v > 0, "attendu : un nombre > 0"),
    "vad.debut_fenetre": (lambda v: v >= 1, "attendu : un entier >= 1"),
    "vad.debut_trames_voisees": (lambda v: v >= 1, "attendu : un entier >= 1"),
    "live.retard_alerte_s": (lambda v: v > 0, "attendu : un nombre > 0"),
    "drive.taille_bloc_mo": (lambda v: v >= 1, "attendu : un entier >= 1"),
    "regles.reflexion": (lambda v: v in ("auto", "minimal", "low", "medium", "high"),
                         "attendu : auto, minimal, low, medium ou high"),
    "regles.nb_phrases": (lambda v: 1 <= v <= 50, "attendu : un entier de 1 à 50"),
    "regles.temperature": (lambda v: 0 <= v <= 2, "attendu : un nombre de 0 à 2"),
    "regles.delai_attente_s": (lambda v: v >= 5, "attendu : un entier >= 5"),
    "regles.delai_min_s": (lambda v: v >= 0, "attendu : un entier >= 0"),
    "regles.jeu_par_defaut": (lambda v: bool(v), "ne peut pas être vide"),
    "regles.modele": (lambda v: bool(v), "ne peut pas être vide"),
    "live.historique": (lambda v: v >= 1, "attendu : un entier >= 1"),
    "whisper.filtre_hallucinations": (lambda v: bool(re.compile(v)) or True, "expression régulière invalide"),
}


def parse_setting(key: str, raw):
    """Convertit une valeur texte selon le type du paramètre ; ValueError (message lisible) si invalide."""
    typ = SPEC[key][1]
    if typ is str:
        v = str(raw).strip()
        if key in ("fin_de_session.partage", "regles.reflexion"):
            v = v.lower()
    else:
        r = str(raw).strip().lower()
        if typ is bool:
            if r in _TRUE:
                v = True
            elif r in _FALSE:
                v = False
            else:
                raise ValueError("attendu : oui ou non")
        else:
            try:
                v = typ(r.replace(",", ".") if typ is float else r)
            except ValueError:
                raise ValueError("attendu : un nombre" + (" entier" if typ is int else "")) from None
    check = CHECKS.get(key)
    if check:
        try:
            ok = check[0](v)
        except Exception:
            ok = False
        if not ok:
            raise ValueError(check[1])
    return v


def format_setting(v) -> str:
    return "oui" if v is True else "non" if v is False else str(v)


def ini_set(path: Path, section: str, key: str, value: str, comment: str = ""):
    """Écrit `key = value` dans la section, EN PLACE : les commentaires et le reste du fichier sont conservés."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    sec_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*[=:]")
    start, end = None, len(lines)
    for i, line in enumerate(lines):
        m = sec_re.match(line)
        if m:
            if start is not None:
                end = i
                break
            if m.group(1).strip().lower() == section.lower():
                start = i
    new_line = f"{key} = {value}"
    cm = [f"# {c}" for c in comment.splitlines()] if comment else []
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section}]"] + cm + [new_line]
    else:
        for i in range(start + 1, end):
            if key_re.match(lines[i]):
                lines[i] = new_line
                break
        else:
            ins = end
            while ins > start + 1 and not lines[ins - 1].strip():
                ins -= 1
            lines[ins:ins] = [""] + cm + [new_line]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_config_text(overrides: dict | None = None) -> str:
    overrides = overrides or {}
    out = ["# Configuration du bot Discord d'enregistrement vocal.",
           "# Modifie les valeurs puis relance le programme (les étapes de [fin_de_session] sont relues à chaque /stop).",
           "# Booléens : oui / non. Les lignes commençant par # sont des commentaires.",
           "# Une variable d'environnement historique (ex. WHISPER_MODEL) prime sur ce fichier si elle est définie.", ""]
    for section, title in SECTION_TITLES.items():
        out += [f"# ---- {title} ----", f"[{section}]"]
        for key, typ, default, env, desc in SETTINGS:
            if not key.startswith(section + "."):
                continue
            out += [f"# {c}" for c in desc.splitlines()]
            if env:
                out.append(f"# (variable d'environnement équivalente : {env})")
            out.append(f"{key.split('.', 1)[1]} = {format_setting(overrides.get(key, default))}")
            out.append("")
    return "\n".join(out)


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.values: dict = {}
        self._lock = threading.RLock()
        self._warned: set = set()
        self._bootstrap()
        self.reload()

    def _bootstrap(self):
        """Crée le fichier s'il manque ; sinon y ajoute les paramètres apparus depuis sa création."""
        if not self.path.exists():
            overrides = {}
            legacy = Path("bot_config.json")          # config des étapes de la v2 : on la récupère
            if legacy.exists():
                try:
                    for k, v in json.loads(legacy.read_text(encoding="utf-8")).items():
                        if f"fin_de_session.{k}" in SPEC:
                            overrides[f"fin_de_session.{k}"] = parse_setting(f"fin_de_session.{k}", v)
                except (OSError, ValueError):
                    pass
            self.path.write_text(default_config_text(overrides), encoding="utf-8")
            print(f"[config] {self.path} créé avec les valeurs par défaut"
                  + (" (étapes reprises de bot_config.json)" if overrides else ""))
            return
        cp = self._parse()
        if cp is None:
            return
        missing = [s for s in SETTINGS if not cp.has_option(*s[0].split(".", 1))]
        for key, typ, default, env, desc in missing:
            section, name = key.split(".", 1)
            ini_set(self.path, section, name, format_setting(default), desc)
        if missing:
            print(f"[config] {len(missing)} paramètre(s) ajouté(s) à {self.path} : "
                  + ", ".join(m[0] for m in missing[:8]) + (" …" if len(missing) > 8 else ""))

    def _parse(self):
        cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None)
        try:
            cp.read_string(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, configparser.Error) as e:
            print(f"[config] {self.path} illisible ({e}) : valeurs par défaut utilisées")
            return None
        return cp

    def reload(self):
        with self._lock:
            cp = self._parse()
            vals = {}
            for key, typ, default, env, _desc in SETTINGS:
                section, name = key.split(".", 1)
                raw = None
                if env and os.environ.get(env, "") != "":
                    raw = os.environ[env]
                elif cp is not None and cp.has_option(section, name):
                    raw = cp.get(section, name)
                try:
                    vals[key] = default if raw is None else parse_setting(key, raw)
                except ValueError as e:
                    if (key, raw) not in self._warned:        # un seul avertissement par valeur invalide
                        self._warned.add((key, raw))
                        print(f"[config] {key} = {raw!r} invalide ({e}) : valeur par défaut « {default} » utilisée")
                    vals[key] = default
            self.values = vals

    def __getitem__(self, key: str):
        return self.values[key]

    def set(self, key: str, value):
        with self._lock:
            section, name = key.split(".", 1)
            ini_set(self.path, section, name, format_setting(value), SPEC[key][4])
            self.values[key] = value


def _config_path() -> Path:
    if "--config" in sys.argv:
        i = sys.argv.index("--config")
        if i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1])
    return Path(os.getenv("BOT_CONFIG", "config.ini"))


SET = Settings(_config_path())


# --- étapes de fin de session : lues dans la section [fin_de_session] de config.ini, modifiables avec /config ---

CONFIG_KEYS = ["mp3", "mp3_kbps", "rapport", "drive", "drive_audio", "drive_transcript", "drive_rapport", "partage"]
CONFIG_SPEC = {k: (SPEC[f"fin_de_session.{k}"][1], SPEC[f"fin_de_session.{k}"][2], SPEC[f"fin_de_session.{k}"][4])
               for k in CONFIG_KEYS}


def parse_config_value(key: str, raw: str):
    return parse_setting(f"fin_de_session.{key}", raw)


def load_config() -> dict:
    SET.reload()                           # relit config.ini : les changements faits à la main sont pris en compte
    return {k: SET[f"fin_de_session.{k}"] for k in CONFIG_KEYS}


def save_config(cfg: dict):
    for k in CONFIG_KEYS:                  # n'écrit que ce qui a changé, en préservant les commentaires du fichier
        if cfg[k] != SET[f"fin_de_session.{k}"]:
            SET.set(f"fin_de_session.{k}", cfg[k])
