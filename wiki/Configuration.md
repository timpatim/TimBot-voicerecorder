# Configuration (`config.ini`)

> Page générée par `python tools/make_wiki.py` à partir de `voicebot/config.py` : ne la modifiez pas à la main.

Au premier lancement, le bot crée `config.ini` avec toutes les valeurs par défaut et tous les commentaires
([`config.example.ini`](https://github.com/timpatim/TimBot-voicerecorder/blob/main/config.example.ini) en est une copie). Les clés manquantes sont ajoutées
automatiquement ; une valeur invalide retombe sur le défaut avec un avertissement dans la console.

- Les booléens s'écrivent `oui` / `non` (`true`, `1`, `on`… sont aussi acceptés).
- Le fichier est relu au démarrage. Seules les étapes de `[fin_de_session]` sont relues à chaque `/stop`, et peuvent
  se modifier depuis Discord avec [`/config`](Commandes#config).
- Mieux vaut ne pas écrire de secret dans le fichier : `token` et `cle_api` peuvent venir des variables d'environnement
  `DISCORD_TOKEN` et `GEMINI_API_KEY`. Ne commitez jamais `config.ini`.


## Sections

- [`[discord]`](#discord) : Discord
- [`[modeles]`](#modeles) : Modèles téléchargés (Whisper, OmniVoice, Chatterbox...)
- [`[sessions]`](#sessions) : Enregistrements
- [`[audio]`](#audio) : Archivage audio
- [`[vad]`](#vad) : Détection de voix (VAD) et découpage en énoncés
- [`[whisper]`](#whisper) : Transcription WhisperX
- [`[live]`](#live) : Suivi en direct (console, statut du bot, file d'attente)
- [`[stats]`](#stats) : Statistiques
- [`[rapport]`](#rapport) : Rapport PDF
- [`[fin_de_session]`](#fin_de_session) : Traitement de fin de session (modifiable avec /config pour les étapes)
- [`[mp3]`](#mp3) : Conversion MP3
- [`[drive]`](#drive) : Google Drive
- [`[regles]`](#regles) : Google Gemini : questions de règles (/regle) et génération du contexte de campagne (/campagne)
- [`[campagnes]`](#campagnes) : Campagnes de jeu de rôle (commande /campagne)
- [`[claude]`](#claude) : Claude (ton abonnement, via le programme `claude`) : résumés de campagne avec /campagne resumer
- [`[voix]`](#voix) : Voix du bot (OmniVoice ou Chatterbox) : « Dans l'épisode précédent… » dicté dans le canal vocal

## discord

*Discord*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `token` | texte | *(vide)* |  | Jeton du bot. Mieux vaut le laisser vide et définir la variable d'environnement DISCORD_TOKEN. (variable d'environnement : `DISCORD_TOKEN`) |
| `guild_id` | texte | *(vide)* |  | Identifiant de ton serveur : les commandes apparaissent instantanément (vide = commandes globales, plus lentes). (variable d'environnement : `DISCORD_GUILD_ID`) |
| `canal_messages` | texte | *(vide)* | l'identifiant numérique du salon (ou vide) | Identifiant du salon texte des messages SYSTÈME et informatifs : suivi du traitement de fin de session, alertes, une ligne de trace à chaque commande lancée par un joueur, compte rendu et fichiers de /campagne resumer, integrer et pack sortie:fichier. Les réponses aux demandes (/regle, /transcript, /stats, /campagne afficher...) restent dans le salon où la commande est tapée. Vide = tout dans le salon de la commande (pas de trace). Pour l'obtenir : Paramètres Discord > Avancés > Mode développeur, puis clic droit sur le salon > Copier l'identifiant du salon. Le bot doit pouvoir y voir le salon, écrire et intégrer des liens. |
| `message_consentement` | texte | `🔴 **Enregistrement en cours** dans {canal}. L'audio de chaque participant et sa transcription sont conservés. Quitte le canal si tu ne souhaites pas être enregistré. `/stop` pour terminer.` |  | Message affiché au /record. {canal} est remplacé par le canal vocal. |

## modeles

*Modèles téléchargés (Whisper, OmniVoice, Chatterbox...)*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `dossier` | texte | `modeles` |  | Dossier où sont téléchargés les modèles (Whisper, alignement, voix), relatif au dossier du programme. Vide = cache habituel de Hugging Face et PyTorch (~/.cache). Les modèles déjà téléchargés ailleurs ne sont pas déplacés : ils sont retéléchargés (plusieurs Go) ; pour les réutiliser, copiez ~/.cache/huggingface/hub dans <dossier>/huggingface/. |

## sessions

*Enregistrements*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `dossier` | texte | `sessions` |  | Dossier où sont créées les sessions. |

## audio

*Archivage audio*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `tolerance_trou_ms` | entier | `200` |  | Gigue réseau tolérée (ms) avant de considérer qu'un utilisateur s'est tu et de combler avec du silence. |
| `flush_s` | nombre | `1.0` | un nombre > 0 | Intervalle (s) entre deux écritures sur disque des fichiers WAV. |
| `meta_toutes_les_s` | entier | `10` |  | Intervalle (s) de mise à jour de meta.json pendant l'enregistrement. |

## vad

*Détection de voix (VAD) et découpage en énoncés*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `agressivite` | entier | `2` | un entier de 0 à 3 | webrtcvad, de 0 (permissif) à 3 (strict). Plus strict = moins de bruit détecté comme voix. (variable d'environnement : `VAD_AGGRESSIVENESS`) |
| `seuil_energie_rms` | nombre | `400.0` |  | Seuil RMS du VAD de repli (utilisé seulement si webrtcvad n'est pas installé). (variable d'environnement : `VAD_RMS`) |
| `pre_roll_ms` | entier | `300` |  | Audio conservé (ms) avant le début détecté de la parole. |
| `debut_fenetre` | entier | `5` | un entier >= 1 | Début de parole : nombre de trames de 20 ms examinées... |
| `debut_trames_voisees` | entier | `3` | un entier >= 1 | ... dont au moins ce nombre doivent être voisées. |
| `silence_fin_ms` | entier | `700` |  | Fin d'énoncé après ce silence (ms). |
| `marge_fin_ms` | entier | `200` |  | Audio conservé (ms) après la dernière trame voisée. |
| `enonce_max_s` | entier | `20` |  | Durée maximale (s) d'un énoncé avant découpage forcé. |
| `min_trames_voisees` | entier | `22` |  | Un énoncé a au moins ce nombre de trames voisées (22 = 440 ms), sinon c'est du bruit. |
| `niveau_min_dbfs` | nombre | `-45.0` |  | Un énoncé plus faible que ce niveau RMS (dBFS) n'est pas envoyé à WhisperX. (variable d'environnement : `MIN_UTTERANCE_DBFS`) |

## whisper

*Transcription WhisperX*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `modele` | texte | `small` |  | Modèle WhisperX : tiny, base, small, medium, large-v3... (variable d'environnement : `WHISPER_MODEL`) |
| `device` | texte | *(vide)* |  | cuda ou cpu (vide = détection automatique). (variable d'environnement : `WHISPER_DEVICE`) |
| `calcul` | texte | *(vide)* |  | float16, int8... (vide = float16 sur GPU, int8 sur CPU). (variable d'environnement : `WHISPER_COMPUTE`) |
| `langue` | texte | `fr` |  | Langue parlée. (variable d'environnement : `WHISPER_LANG`) |
| `batch` | entier | `0` |  | Taille de batch (0 = automatique : 8 sur GPU, 1 sur CPU). (variable d'environnement : `WHISPER_BATCH`) |
| `alignement` | oui / non | `oui` |  | Alignement mot à mot : timestamps plus précis, un peu plus lent. (variable d'environnement : `WHISPERX_ALIGN`) |
| `filtre_hallucinations` | texte | `sous-?titr\|amara\.org\|merci d['’]avoir regard\|abonne[zr]-vous` | expression régulière invalide | Expression régulière : les segments qui la contiennent sont rejetés (sous-titres inventés sur du silence). |
| `mots_seuls_ignores` | texte | `merci, au revoir, bye, amen` |  | Mots (séparés par des virgules ou des espaces) : une phrase qui ne contient QUE ces mots, éventuellement répétés (« Merci. », « merci, merci ! »), est rejetée comme bruit : Whisper invente ces mots sur les bruits de fond. « Merci beaucoup » est conservé. Vide = rien d'ignoré. |

## live

*Suivi en direct (console, statut du bot, file d'attente)*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `debug` | oui / non | `non` |  | Trace chaque énoncé (durée, niveau, décision) pour régler les seuils. (variable d'environnement : `LIVE_DEBUG`) |
| `statut_console_s` | nombre | `10.0` |  | Secondes entre deux lignes de suivi en console pendant un REC (0 = seulement les changements d'état). (variable d'environnement : `LIVE_STATUS_INTERVAL`) |
| `retard_alerte_s` | nombre | `30.0` | un nombre > 0 | Secondes d'audio en attente qui remplissent la barre et déclenchent l'alerte. (variable d'environnement : `ASR_BACKLOG_WARN`) |
| `historique` | entier | `500` | un entier >= 1 | Nombre de messages gardés en mémoire pour /transcript. |
| `statut_discord_texte_s` | nombre | `15.0` |  | Délai minimal (s) entre deux changements de texte du statut Discord (limite de Discord). |
| `statut_scrutation_s` | nombre | `1.0` | un nombre > 0 | Fréquence (s) de vérification de l'état pour le statut et la console. |
| `attente_fin_asr_s` | entier | `300` |  | À /stop : attente maximale (s) que WhisperX vide sa file. |

## stats

*Statistiques*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `fenetre_interruption_s` | nombre | `30.0` |  | Interruptions : on cherche l'intervenant coupé dans cette fenêtre (s). |
| `chevauchement_interruption_s` | nombre | `0.3` |  | Chevauchement minimal (s) pour compter une interruption. |
| `pause_monologue_s` | nombre | `3.0` |  | Pause maximale (s) entre deux segments d'un même monologue. |
| `monologue_min_s` | nombre | `8.0` |  | Durée minimale (s) pour afficher le plus long monologue. |
| `parole_min_mots_min_s` | nombre | `10.0` |  | Temps de parole minimal (s) pour calculer les mots/min. |
| `silence_affiche_min_s` | nombre | `10.0` |  | Durée minimale (s) pour afficher le plus long silence. |
| `mot_favori_min` | entier | `3` |  | Occurrences minimales pour qu'un mot soit « favori ». |
| `nb_mots_top` | entier | `5` |  | Nombre de mots dans « mots les plus utilisés ». |
| `lignes_tableau_discord` | entier | `15` |  | Intervenants affichés dans le tableau Discord. |
| `mots_vides_supplementaires` | texte | *(vide)* |  | Mots à ignorer en plus de la liste intégrée pour les stats de vocabulaire (séparés par des espaces ou virgules). |

## rapport

*Rapport PDF*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `max_intervenants_graphiques` | entier | `12` |  | Intervenants affichés dans les graphiques du PDF. |

## fin_de_session

*Traitement de fin de session (modifiable avec /config pour les étapes)*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `mp3` | oui / non | `oui` |  | Convertir les fichiers WAV en MP3 |
| `mp3_kbps` | entier | `64` | un débit entre 32 et 320 | Débit MP3 en kbit/s (voix mono : 48 à 96 suffit) |
| `rapport` | oui / non | `oui` |  | Générer le rapport PDF (avec les statistiques) |
| `drive` | oui / non | `oui` |  | Déposer les fichiers sur Google Drive |
| `drive_audio` | oui / non | `oui` |  | Drive : inclure l'audio (MP3, ou WAV si la conversion est désactivée) |
| `drive_transcript` | oui / non | `oui` |  | Drive : inclure le transcript |
| `nettoyage_mots_ignores` | texte | `euh, heu, hum, hmm, mhm` |  | Transcript nettoyé : mots retirés quand ils forment une réplique à eux seuls (sons sans sens). « oui », « non », « ok » sont conservés : ce sont des réponses. |
| `nettoyage_fusion_s` | nombre | `0.0` | un nombre de 0 à 60 | Transcript nettoyé : fusionne les répliques consécutives d'un même locuteur séparées de moins de N secondes (0 = jamais : chaque réplique garde son horodatage et son locuteur). |
| `drive_transcript_brut` | oui / non | `non` |  | Drive : déposer aussi le transcript brut (par défaut, seul le transcript nettoyé est déposé ; le brut reste en local). |
| `drive_rapport` | oui / non | `oui` |  | Drive : inclure le rapport PDF |
| `partage` | texte | `lien` | lien ou aucun | Drive : « lien » = toute personne ayant le lien peut consulter, « aucun » = privé |
| `commande_retraitement` | texte | *(vide)* |  | Commande lancée en fin de session, ex. python reprocess.py (reçoit le dossier de session en argument). (variable d'environnement : `REPROCESS_CMD`) |

## mp3

*Conversion MP3*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `ffmpeg` | texte | `ffmpeg` |  | Chemin de ffmpeg s'il n'est pas dans le PATH. (variable d'environnement : `FFMPEG`) |

## drive

*Google Drive*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `identifiants` | texte | `credentials.json` |  | Client OAuth « application de bureau ». (variable d'environnement : `GOOGLE_CREDENTIALS`) |
| `jeton` | texte | `token.json` |  | Jeton Google mis en cache après la première autorisation. (variable d'environnement : `GOOGLE_TOKEN`) |
| `dossier_racine` | texte | `Enregistrements Discord` |  | Dossier racine sur le Drive. (variable d'environnement : `DRIVE_ROOT_NAME`) |
| `attente_connexion_s` | entier | `180` |  | Attente maximale (s) de la connexion Google avant échec de l'étape. |
| `taille_bloc_mo` | entier | `8` | un entier >= 1 | Taille des blocs d'envoi (Mo). |

## regles

*Google Gemini : questions de règles (/regle) et génération du contexte de campagne (/campagne)*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `activer` | oui / non | `oui` |  | Active la commande /regle. |
| `cle_api` | texte | *(vide)* |  | Clé d'API Google Gemini (https://aistudio.google.com/apikey). Mieux vaut la laisser vide et définir la variable d'environnement GEMINI_API_KEY. (variable d'environnement : `GEMINI_API_KEY`) |
| `modele` | texte | `gemini-3.6-flash` | ne peut pas être vide | Modèle Gemini. « gemini-flash-latest » suit automatiquement la dernière version Flash. |
| `jeu_par_defaut` | texte | `Pathfinder première édition` | ne peut pas être vide | Jeu de repli : utilisé par /regle quand elle est lancée sans préciser le jeu et qu'aucune campagne n'est active (sinon /regle prend le jeu de la campagne en cours), et pour une nouvelle campagne créée sans jeu. |
| `nb_phrases` | entier | `15` | un entier de 1 à 50 | Nombre de dernières phrases du transcript envoyées à Gemini (1 à 50). |
| `question_max` | entier | `1500` | un entier >= 50 | Longueur maximale (caractères) de la question libre de /regle. |
| `temperature` | nombre | `0.2` | un nombre de 0 à 2 | Créativité de la réponse (0 = très factuel, 2 = très libre). |
| `reflexion` | texte | `low` | auto, minimal, low, medium ou high | Niveau de « réflexion » de Gemini 3.x : low (rapide, recommandé), medium, high (plus lent, souvent plusieurs dizaines de secondes), minimal (pas toujours accepté), auto (ne rien envoyer : valeur par défaut du modèle, à utiliser avec un modèle qui ne gère pas ce réglage, comme gemini-2.5-flash). |
| `recherche_web` | oui / non | `non` |  | Autorise Gemini à s'appuyer sur la recherche Google pour vérifier la règle (plus fiable, plus lent). |
| `delai_attente_s` | entier | `60` | un entier >= 5 | Temps maximal d'attente de la réponse de Gemini (s). |
| `delai_min_s` | entier | `10` | un entier >= 0 | Délai minimal (s) entre deux /regle sur un même serveur (évite les abus). |
| `instruction` | texte | `Tu es arbitre et expert des règles du jeu « {jeu} ». Tu reçois les dernières phrases échangées à l'oral par des joueurs autour de la table (transcription automatique : erreurs, noms propres déformés, phrases coupées possibles). Repère le point de règle dont ils parlent ou sur lequel ils hésitent, puis : 1) formule-le en une phrase ; 2) explique la règle précisément, avec conditions, exceptions et cas limites utiles ; 3) tranche le cas concret évoqué s'il y en a un ; 4) cite la source (livre, chapitre) seulement si tu en es sûr. Si tu n'es pas certain, ou si plusieurs interprétations existent (errata, différences entre éditions, règle optionnelle), dis-le clairement plutôt que d'inventer. Réponds en français, de façon concise (moins de 1500 caractères), sans introduction ni formule de politesse. Si aucune question de règle ne ressort, dis-le en une phrase.` |  | Consigne envoyée à Gemini. {jeu} est remplacé par le nom du jeu. Doit tenir sur une seule ligne. |
| `instruction_question` | texte | `Tu es arbitre et expert des règles du jeu « {jeu} ». Un joueur te pose une question de règle précise. Réponds-y directement : 1) la règle, avec conditions, exceptions et cas limites utiles ; 2) tranche le cas concret décrit s'il y en a un ; 3) cite la source (livre, chapitre) seulement si tu en es sûr. Si tu n'es pas certain, ou si plusieurs interprétations existent (errata, différences entre éditions, règle optionnelle), dis-le clairement plutôt que d'inventer. Si la question manque de précision pour trancher, dis ce qu'il faudrait savoir. Réponds en français, de façon concise (moins de 1500 caractères), sans introduction ni formule de politesse.` |  | Consigne envoyée à Gemini quand /regle reçoit une question libre. {jeu} est remplacé par le nom du jeu. Une seule ligne. |

## campagnes

*Campagnes de jeu de rôle (commande /campagne)*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `dossier` | texte | `campagnes` | ne peut pas être vide | Dossier où sont rangées les campagnes (un sous-dossier par campagne : contexte, joueurs, résumés). |
| `taille_max_brut_ko` | entier | `200` | un entier de 1 à 2000 | Taille maximale (Ko) du texte de contexte qu'on peut envoyer à /campagne contexte. |
| `taille_max_transcript_ko` | entier | `5000` | un entier de 1 à 20000 | Taille maximale (Ko) d'un transcript joint à /campagne resumer. |
| `resume_auto` | oui / non | `non` |  | À la fin d'une session, génère automatiquement le compte rendu, le résumé global et le « précédemment » (Gemini). |
| `presse_papiers` | oui / non | `oui` |  | /campagne pack copie le pack dans le presse-papiers et /campagne integrer lit la réponse dans le presse-papiers (sans fichier). Il s'agit du presse-papiers de la machine qui fait tourner le bot : à mettre sur non si le bot tourne sur un autre ordinateur que ton client Discord (utilise alors les fichiers). |
| `moteur_resume` | texte | `claude` | claude ou gemini | Moteur de /campagne resumer : claude (programme `claude` de ton abonnement, voir [claude]) ou gemini (ancienne méthode, plusieurs appels, clé Gemini requise). |
| `pack_fusion_s` | nombre | `10.0` | un nombre de 0 à 60 | Pack allégé : fusionne les répliques consécutives d'un même locuteur séparées de moins de N secondes (moins de texte à envoyer, donc moins d'usage de l'abonnement ; 0 = aucune fusion). Seule la première réplique d'un bloc garde son horodatage. |
| `pack_minutes` | entier | `0` | 0 (pas de découpe) ou un entier de 15 à 240 | Découpe des packs : durée (minutes) d'une partie, 0 = un seul pack par séance. À activer (ex. 60) si le chat utilisé ne lit pas les longs fichiers jusqu'au bout. |
| `tranche_minutes` | entier | `30` | un entier de 5 à 120 | Durée (minutes) des tranches de transcript analysées une à une. |
| `delai_attente_s` | entier | `300` | un entier >= 30 | Temps maximal d'attente (s) d'une réponse de Gemini pour la mémoire de campagne. |

## claude

*Claude (ton abonnement, via le programme `claude`) : résumés de campagne avec /campagne resumer*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `commande` | texte | `claude` | ne peut pas être vide | Programme Claude Code à lancer : « claude » s'il est dans le PATH, sinon son chemin complet. Il doit être connecté à ton abonnement (ouvre un terminal : `claude`, puis `/login`). Aucune clé d'API n'est utilisée : ANTHROPIC_API_KEY est même retirée de l'environnement de l'appel. |
| `modele` | texte | `sonnet` | ne peut pas être vide | Modèle utilisé : sonnet (recommandé : bon rapport qualité / usage), opus (plus coûteux en usage), haiku (rejeté lors des essais : invente des détails), ou un nom complet de modèle. |
| `effort` | texte | `medium` | low, medium, high, xhigh ou max | Effort de réflexion : medium (recommandé : plus fiable, ~2 min par séance), low (plus rapide, moins fiable : il a raccourci le résumé lors des essais), high, xhigh ou max (plus lent et plus d'usage de l'abonnement). |
| `delai_s` | entier | `900` | un entier >= 60 | Temps maximal d'attente (s) d'une réponse de Claude. |
| `options` | texte | `--safe-mode` |  | Options supplémentaires de la ligne de commande (ignore CLAUDE.md, hooks, plugins et réglages perso de ton Claude Code). Si ta version de Claude Code ne connaît pas --safe-mode (erreur « unknown option »), vide ce réglage ou mets à jour : `claude update`. |

## voix

*Voix du bot (OmniVoice ou Chatterbox) : « Dans l'épisode précédent… » dicté dans le canal vocal*

| Clé | Type | Défaut | Valeurs admises | Rôle |
|---|---|---|---|---|
| `activer` | oui / non | `non` |  | Active la voix du bot (nécessite install_voix.bat). Désactivée : aucune synthèse vocale, aucune lecture. |
| `liberer_whisper` | oui / non | `non` |  | oui : Whisper est retiré de la mémoire (GPU) pendant une synthèse vocale puis rechargé, pour ne jamais avoir les deux modèles en même temps ; la synthèse, /dire et /campagne audio sont alors refusés pendant un enregistrement. non : Whisper reste chargé, la transcription continue et la voix fonctionne aussi pendant un enregistrement, mais il faut assez de mémoire GPU pour les deux (voir la ligne de mémoire GPU dans la console). |
| `au_demarrage` | oui / non | `oui` |  | Au /record, le bot dicte dans le canal vocal le « précédemment » de la dernière séance (audio préparé à l'avance). |
| `moteur` | texte | `omnivoice` | omnivoice ou chatterbox | Moteur de synthèse : omnivoice (install_omnivoice.bat) ou chatterbox (install_voix.bat). Changer de moteur invalide les audios déjà préparés. |
| `python` | texte | *(vide)* |  | Python de l'environnement de la voix (vide = celui écrit par l'installeur du moteur choisi, sinon celui du bot). |
| `langue` | texte | `fr` | ne peut pas être vide | Langue lue (code à deux lettres : fr, en, es...). |
| `dossier` | texte | `voix` |  | Dossier commun des voix du bot : un échantillon audio (~10 s) par voix, nommé voix.<nom>.wav (ou .mp3, .flac). Avec OmniVoice, voix.<nom>.txt donne la transcription de l'échantillon ; sans lui, OmniVoice la devine avec Whisper. La voix d'une campagne se choisit avec /voix, une voix ponctuelle avec le paramètre nom_voix de /dire et /campagne audio. À n'utiliser qu'avec l'accord de la personne dont c'est la voix. |
| `voix_reference` | texte | *(vide)* |  | Ancien réglage, toujours pris en compte : échantillon par défaut quand la campagne n'a pas de voix choisie (/voix). Une campagne peut aussi avoir un voix.wav dans son dossier (ancien système). Vide = voix par défaut du moteur. |
| `device` | texte | *(vide)* | cuda, cpu ou vide | cuda ou cpu (vide = automatique). |
| `vitesse` | nombre | `1.0` | un nombre de 0.5 à 2.0 | Vitesse de lecture (1.0 = normale, 1.15 = un peu plus rapide). Appliquée sans changer la hauteur de la voix. Entre 0.5 et 2.0. |
| `pause_s` | nombre | `0.25` | un nombre de 0 à 3 | Silence (s) entre deux morceaux de texte (avant accélération). |
| `etapes` | entier | `32` | un entier de 4 à 128 | OmniVoice : nombre d'étapes de génération (32 = qualité recommandée, 16 = plus rapide). Sans effet avec Chatterbox. |
| `temperature` | nombre | `0.6` | un nombre de 0.1 à 1.5 | Chatterbox uniquement. Variabilité de la synthèse (0.8 = défaut de Chatterbox). 0.6 limite les mots farfelus et les bruits ; trop bas, la voix devient monotone. Si un audio est raté, relance avec /campagne audio force. |
| `mots` | entier | `110` | un entier de 30 à 400 | Longueur visée (en mots) du « précédemment » demandé à l'IA dans les prompts. ~110 mots = environ 40 s à voix haute. Ne change pas un texte déjà écrit. |
| `dire_max` | entier | `600` | un entier de 20 à 3000 | Longueur maximale (caractères) d'un texte dit avec /dire (environ 40 s de voix pour 600 caractères). |
| `delai_s` | entier | `900` | un entier >= 30 | Temps maximal (s) d'une synthèse vocale. |
