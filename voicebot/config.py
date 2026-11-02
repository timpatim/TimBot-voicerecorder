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
    "regles": "Google Gemini : questions de règles (/regle) et génération du contexte de campagne (/campagne)",
    "campagnes": "Campagnes de jeu de rôle (commande /campagne)",
    "voix": "Voix du bot (Chatterbox) : « Dans l'épisode précédent… » dicté dans le canal vocal",
}

# (section.clé, type, défaut, ancienne variable d'environnement, description)
SETTINGS = [
    ("discord.token", str, "", "DISCORD_TOKEN",
     "Jeton du bot. Mieux vaut le laisser vide et définir la variable d'environnement DISCORD_TOKEN."),
    ("discord.guild_id", str, "", "DISCORD_GUILD_ID",
     "Identifiant de ton serveur : les commandes apparaissent instantanément (vide = commandes globales, plus lentes)."),
    ("discord.canal_messages", str, "", None,
     "Identifiant du salon texte où le bot publie ses messages (suivi du traitement de fin de session, résultats de\n"
     "/transcript, /regle, /stats, /config). Vide = le salon où la commande est lancée. Pour l'obtenir : Paramètres\n"
     "Discord > Avancés > Mode développeur, puis clic droit sur le salon > Copier l'identifiant du salon.\n"
     "Le bot doit pouvoir y voir le salon, écrire et intégrer des liens."),
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
    ("fin_de_session.nettoyage_mots_ignores", str, "euh, heu, hum, hmm, mhm", None,
     "Transcript nettoyé : mots retirés quand ils forment une réplique à eux seuls (sons sans sens). "
     "« oui », « non », « ok » sont conservés : ce sont des réponses."),
    ("fin_de_session.nettoyage_fusion_s", float, 0.0, None,
     "Transcript nettoyé : fusionne les répliques consécutives d'un même locuteur séparées de moins de N secondes "
     "(0 = jamais : chaque réplique garde son horodatage et son locuteur)."),
    ("fin_de_session.drive_transcript_brut", bool, False, None,
     "Drive : déposer aussi le transcript brut (par défaut, seul le transcript nettoyé est déposé ; le brut reste en local)."),
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
    ("voix.activer", bool, False, None,
     "Active la voix du bot (nécessite install_voix.bat). Désactivée : aucune synthèse vocale, aucune lecture."),
    ("voix.au_demarrage", bool, True, None,
     "Au /record, le bot dicte dans le canal vocal le « précédemment » de la dernière séance (audio préparé à l'avance)."),
    ("voix.python", str, "", None,
     "Python de l'environnement de la voix (vide = celui écrit par install_voix.bat, sinon celui du bot)."),
    ("voix.langue", str, "fr", None, "Langue lue (code Chatterbox : fr, en, es...)."),
    ("voix.voix_reference", str, "", None,
     "Échantillon audio (~10 s) de la voix à imiter, valable pour toutes les campagnes ; une campagne peut avoir sa propre "
     "voix en plaçant voix.wav dans son dossier. Vide et sans voix.wav = voix par défaut. "
     "À n'utiliser qu'avec l'accord de la personne dont c'est la voix."),
    ("voix.device", str, "", None, "cuda ou cpu (vide = automatique)."),
    ("voix.vitesse", float, 1.15, None,
     "Vitesse de lecture (1.0 = normale, 1.15 = un peu plus rapide). Appliquée sans changer la hauteur de la voix. "
     "Entre 0.5 et 2.0."),
    ("voix.pause_s", float, 0.25, None, "Silence (s) entre deux morceaux de texte (avant accélération)."),
    ("voix.temperature", float, 0.6, None,
     "Variabilité de la synthèse (0.8 = défaut de Chatterbox). 0.6 limite les mots farfelus et les bruits ; "
     "trop bas, la voix devient monotone. Si un audio est raté, relance avec /campagne audio force."),
    ("voix.mots", int, 110, None,
     "Longueur visée (en mots) du « précédemment » demandé à l'IA dans les prompts. ~110 mots = environ 40 s à voix haute. "
     "Ne change pas un texte déjà écrit."),
    ("voix.delai_s", int, 900, None, "Temps maximal (s) d'une synthèse vocale."),
    ("campagnes.dossier", str, "campagnes", None,
     "Dossier où sont rangées les campagnes (un sous-dossier par campagne : contexte, joueurs, résumés)."),
    ("campagnes.taille_max_brut_ko", int, 200, None,
     "Taille maximale (Ko) du texte de contexte qu'on peut envoyer à /campagne contexte."),
    ("campagnes.taille_max_transcript_ko", int, 5000, None,
     "Taille maximale (Ko) d'un transcript joint à /campagne resumer."),
    ("campagnes.resume_auto", bool, False, None,
     "À la fin d'une session, génère automatiquement le compte rendu, le résumé global et le « précédemment » (Gemini)."),
    ("campagnes.pack_auto", bool, True, None,
     "À la fin d'une session avec une campagne active, prépare les packs de prompts à coller dans Gemini/Claude (aucun appel d'API)."),
    ("campagnes.pack_minutes", int, 0, None,
     "Découpe des packs : durée (minutes) d'une partie, 0 = un seul pack par séance. À activer (ex. 60) si le chat utilisé "
     "ne lit pas les longs fichiers jusqu'au bout."),
    ("campagnes.tranche_minutes", int, 30, None, "Durée (minutes) des tranches de transcript analysées une à une."),
    ("campagnes.delai_attente_s", int, 300, None, "Temps maximal d'attente (s) d'une réponse de Gemini pour la mémoire de campagne."),
    ("regles.activer", bool, True, None, "Active la commande /regle."),
    ("regles.cle_api", str, "", "GEMINI_API_KEY",
     "Clé d'API Google Gemini (https://aistudio.google.com/apikey). Mieux vaut la laisser vide\n"
     "et définir la variable d'environnement GEMINI_API_KEY."),
    ("regles.modele", str, "gemini-3.6-flash", None,
     "Modèle Gemini. « gemini-flash-latest » suit automatiquement la dernière version Flash."),
    ("regles.jeu_par_defaut", str, "Pathfinder première édition", None,
     "Jeu de repli : utilisé par /regle quand elle est lancée sans préciser le jeu et qu'aucune campagne n'est active\n"
     "(sinon /regle prend le jeu de la campagne en cours), et pour une nouvelle campagne créée sans jeu."),
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
    "discord.canal_messages": (lambda v: v == "" or v.isdigit(), "attendu : l'identifiant numérique du salon (ou vide)"),
    "regles.reflexion": (lambda v: v in ("auto", "minimal", "low", "medium", "high"),
                         "attendu : auto, minimal, low, medium ou high"),
    "voix.langue": (lambda v: bool(v), "ne peut pas être vide"),
    "voix.device": (lambda v: v in ("", "cuda", "cpu"), "attendu : cuda, cpu ou vide"),
    "voix.vitesse": (lambda v: 0.5 <= v <= 2.0, "attendu : un nombre de 0.5 à 2.0"),
    "voix.pause_s": (lambda v: 0 <= v <= 3, "attendu : un nombre de 0 à 3"),
    "voix.temperature": (lambda v: 0.1 <= v <= 1.5, "attendu : un nombre de 0.1 à 1.5"),
    "voix.mots": (lambda v: 30 <= v <= 400, "attendu : un entier de 30 à 400"),
    "voix.delai_s": (lambda v: v >= 30, "attendu : un entier >= 30"),
    "campagnes.dossier": (lambda v: bool(v), "ne peut pas être vide"),
    "campagnes.taille_max_brut_ko": (lambda v: 1 <= v <= 2000, "attendu : un entier de 1 à 2000"),
    "campagnes.taille_max_transcript_ko": (lambda v: 1 <= v <= 20000, "attendu : un entier de 1 à 20000"),
    "fin_de_session.nettoyage_fusion_s": (lambda v: 0 <= v <= 60, "attendu : un nombre de 0 à 60"),
    "campagnes.pack_minutes": (lambda v: v == 0 or 15 <= v <= 240, "attendu : 0 (pas de découpe) ou un entier de 15 à 240"),
    "campagnes.tranche_minutes": (lambda v: 5 <= v <= 120, "attendu : un entier de 5 à 120"),
    "campagnes.delai_attente_s": (lambda v: v >= 30, "attendu : un entier >= 30"),
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
