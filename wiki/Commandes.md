# Commandes

Toutes les commandes sont des commandes slash, utilisables sur un serveur (pas en message privé). Les réponses marquées
« privé » ne sont visibles que par la personne qui lance la commande ; les autres sont publiées **dans le salon où la
commande est tapée**. Le salon `canal_messages` de [`[discord]`](Configuration#discord), s'il est renseigné, ne reçoit que
les messages système et informatifs : suivi de fin de session, alertes, **une ligne de trace à chaque commande lancée**
(`▶️ Nom : /commande paramètres`, sans le contenu des fichiers joints), le compte rendu de `/campagne resumer` et
`integrer`, et les fichiers de `/campagne pack sortie:fichier`. Les réponses de `/regle`, `/transcript`, `/stats` et
`/campagne afficher` restent dans le salon de la commande ; `/config` et `/campagne rollback` répondent en privé.

> Les commandes n'apparaissent parfois qu'après un rafraîchissement du client Discord (**Ctrl+R**). Pour une
> synchronisation instantanée, renseignez `guild_id` dans `[discord]`.

| Commande | Rôle | Permission |
|---|---|---|
| [`/record`](#record) | Démarre l'enregistrement du canal vocal | tous |
| [`/stop`](#stop) | Arrête et lance le traitement de fin de session | tous |
| [`/status`](#status) | État de l'enregistrement, de la file de transcription et de Drive | tous |
| [`/stats`](#stats) | Statistiques de la session en cours | tous |
| [`/retranscrire`](#retranscrire) | Refait le transcript d'une session depuis ses fichiers audio | *Gérer le serveur* |
| [`/transcript`](#transcript) | Derniers messages transcrits | tous |
| [`/dire`](#dire) | Le bot dit un texte à voix haute dans votre canal vocal | *Gérer le serveur* |
| [`/regle`](#regle) | Explication d'une règle de jeu par Gemini | tous |
| [`/config`](#config) | Affiche ou modifie le traitement de fin de session | modifier : *Gérer le serveur* |
| [`/campagne`](#campagne) | Campagnes de jeu de rôle, mémoire et voix | voir le tableau des actions |

Voir aussi l'[arbre des commandes](#arbre-des-commandes) pour les paramètres valides de chaque action.

## Arbre des commandes

Discord propose **tous** les paramètres de `/campagne` quelle que soit l'action choisie, y compris ceux qui n'ont aucun
sens pour elle (par exemple `/campagne dicter mj:@quelqu'un`). Un paramètre qui ne concerne pas l'action est simplement
**ignoré**. Cet arbre indique ce qui est réellement utilisé.

Légende : `*` obligatoire · `[ ]` facultatif · `🔒` demande la permission « Gérer le serveur ».

```
/record
/stop
/status
/stats
/retranscrire 🔒  [session]
/transcript  [nombre]
/dire        🔒   texte*  [nom_voix]
/regle       [jeu]  [phrases]  [question]  [dicter [nom_voix]]
/config      [cle [valeur]]            🔒 pour modifier une valeur

/campagne  action:
├─ liste
├─ info             [nom]
├─ afficher         [nom]  [quoi]  [session]  [rubrique]
├─ dicter           [nom]  [session]  [nom_voix]
├─ nouvelle    🔒   nom*  [jeu]  [mj]
├─ choisir     🔒   nom*
├─ mj          🔒   mj*  [nom]
├─ joueur      🔒   membre*  [personnage]  [nom]
├─ contexte    🔒   fichier*  [nom]
├─ journal     🔒   fichier*  [nom]
├─ pack        🔒   [fichier + session*]  [minutes]  [sortie]  [partie]  [nom]
├─ integrer    🔒   [fichier]  [session]  [nom]
├─ resumer     🔒   [fichier + session*]  [nom]
├─ rollback    🔒   [nom]
├─ audio       🔒   [session]  [nom_voix]  [nom]
├─ audio force 🔒   [session]  [nom_voix]  [nom]
└─ voix             [nom_voix]  [nom]        🔒 pour changer la voix (sans `nom_voix` : liste)
```

Précisions :

- **`nom`** désigne la campagne visée. Sans `nom`, c'est la campagne active. Seules `nouvelle` et `choisir` l'exigent.
- **`afficher`** : `quoi` vaut *compte rendu* par défaut, ou *résumé global* si `rubrique` est donnée. `rubrique` ne
  s'applique qu'au résumé global, et `session` seulement au compte rendu et au « précédemment ».
- **`pack`** et **`resumer`** : sans `fichier`, le bot prend la dernière session enregistrée (ou `session`). Avec un
  `fichier` joint, `session` devient **obligatoire** (`2026-10-24` ou `2026-10-24_190000`) : c'est la date de la séance.
- **`pack`** copie le pack dans le **presse-papiers de la machine du bot** (partie par partie avec `partie`) ; `sortie:fichier`
  le publie à la place en fichier(s) dans le salon. Le bot ne peut pas savoir si votre client Discord est sur la même machine :
  si ce n'est pas le cas, mettez `presse_papiers = non` dans [`[campagnes]`](Configuration#campagnes) et utilisez les fichiers.
  Refusé si la machine n'a pas de presse-papiers utilisable.
- **`integrer`** lit la réponse dans le presse-papiers (copiez-la dans le chat avec son bouton « Copier ») ; avec `fichier`,
  elle est lue dans le fichier joint. `session` sert à choisir la séance à laquelle rattacher la réponse ; par défaut, la séance en attente.
- **`audio`** et **`audio force`** : refusées pendant un enregistrement seulement si `liberer_whisper = oui`.
- **`liste`** n'utilise aucun paramètre.

### Tableau croisé

| Action | `nom` | `jeu` | `mj` | `membre` | `personnage` | `quoi` | `rubrique` | `minutes` | `session` | `fichier` | `nom_voix` | `sortie` | `partie` |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `liste` | | | | | | | | | | |  | | |
| `info` | ○ | | | | | | | | | |  | | |
| `afficher` | ○ | | | | | ○ | ○ | | ○ | |  | | |
| `dicter` | ○ | | | | | | | | ○ | | ○ | | |
| `nouvelle` | ● | ○ | ○ | | | | | | | |  | | |
| `choisir` | ● | | | | | | | | | |  | | |
| `mj` | ○ | | ● | | | | | | | |  | | |
| `joueur` | ○ | | | ● | ○ | | | | | |  | | |
| `contexte` | ○ | | | | | | | | | ● |  | | |
| `journal` | ○ | | | | | | | | | ● |  | | |
| `pack` | ○ | | | | | | | ○ | ◐ | ○ |  | ○ | ○ |
| `integrer` | ○ | | | | | | | | ○ | ○ |  | | |
| `resumer` | ○ | | | | | | | | ◐ | ○ |  | | |
| `rollback` | ○ | | | | | | | | | |  | | |
| `audio` | ○ | | | | | | | | ○ | | ○ | | |
| `audio force` | ○ | | | | | | | | ○ | | ○ | | |
| `voix` | ○ | | | | | | | | | | ○ | | |

● obligatoire · ○ facultatif · ◐ obligatoire si un `fichier` est joint · case vide : ignoré.

## Enregistrement

### `/record`

Rejoint **votre** canal vocal et démarre l'enregistrement. Il faut donc être connecté à un salon vocal. Le bot publie
le message de consentement (`message_consentement` dans `[discord]`), qui précise que l'audio **et** la transcription
sont conservés.

Si une campagne est active, le message indique la campagne, le bot écrit `campagne.json` dans le dossier de session et
affiche l'encadré « 📺 Précédemment… » de la dernière séance. Si la voix est activée (`activer` et `au_demarrage` dans
[`[voix]`](Configuration#voix)), le bot lit l'audio du « précédemment » **déjà préparé** ; il ne synthétise jamais
pendant un enregistrement.

Refusée si : vous n'êtes pas dans un canal vocal, un enregistrement est déjà en cours sur le serveur, ou une synthèse
vocale est en cours de préparation (pour ne pas charger deux modèles à la fois).

### `/stop`

Arrête l'enregistrement, quitte le canal vocal et lance le traitement de fin de session. Les étapes sont exécutées
dans l'ordre, une étape en échec n'empêche pas les suivantes, et leur statut s'affiche dans le chat :

1. fin de la transcription (WhisperX vide sa file),
2. transcript final et statistiques,
3. conversion MP3,
4. rapport PDF,
5. mémoire de campagne (seulement si le résumé par API Gemini est activé ; sinon à la demande avec `/campagne resumer` ou `/campagne pack`),
6. envoi sur Google Drive et lien de partage.

Les étapes MP3, rapport, Drive et partage se règlent dans [`[fin_de_session]`](Configuration#fin_de_session) ou avec
[`/config`](#config) ; ce réglage est relu à chaque `/stop`. Refusée s'il n'y a pas d'enregistrement en cours.

### `/status`

Réponse privée, pendant un enregistrement : identifiant et durée de la session, nombre de participants, état de la file
de transcription (une file qui s'allonge signale un retard de WhisperX) et état de Google Drive.

### `/stats`

Statistiques de la session en cours (temps de parole par participant, palmarès), dans un encadré. Nécessite un
enregistrement en cours. Le contenu se règle dans [`[stats]`](Configuration#stats).

### `/retranscrire`

| Paramètre | Rôle | Défaut |
|---|---|---|
| `session` | Session à retranscrire (la liste des sessions qui ont des fichiers audio est proposée) | la dernière enregistrée |

Refait le transcript d'une session déjà terminée **à partir des fichiers audio complets** (un WAV par joueur) : les WAV sont
redécoupés avec les réglages **actuels** de [`[vad]`](Configuration#vad) et [`[whisper]`](Configuration#whisper), puis passés à
WhisperX. Utile après avoir changé un réglage (silence de fin, mots ignorés, modèle…) ou quand la transcription en direct avait
pris du retard. Le suivi s'affiche dans le salon des messages système (ou dans le salon de la commande).

- L'ancien transcript n'est **jamais écrasé** : `transcript_live.jsonl`, `transcript_live.txt`, `transcript_nettoye.txt`,
  `stats.json` et `stats.txt` sont copiés dans `avant_retranscription_<date>/` dans le dossier de la session, avant d'être
  remplacés. Si la retranscription échoue, rien n'est modifié.
- Sont refaits : le transcript brut (`source: rejeu` dans le JSONL), le transcript trié et le transcript nettoyé, les statistiques
  et les segments rejetés. **Ne sont pas refaits** : le MP3, le rapport PDF, le Drive et la mémoire de campagne ; `/campagne resumer`
  refait ensuite le compte rendu à partir du nouveau transcript.
- Comptez environ 10 secondes de calcul par minute de séance sur un GPU comparable à celui d'un PC de jeu (une séance de 3 h :
  de l'ordre de 30 minutes). Un seul travail à la fois.
- Refusée pendant un enregistrement ou une synthèse vocale (WhisperX est partagé), et `/record` est refusé tant qu'elle tourne.

### `/transcript`

| Paramètre | Rôle | Défaut |
|---|---|---|
| `nombre` | Combien de messages afficher (1 à 50) | 10 |

Affiche les derniers messages transcrits, visibles de tous (nécessite un enregistrement en cours). Aucune transcription n'est publiée automatiquement : cette
commande est le seul moyen de les voir pendant la session.

## Voix

### `/dire`

| Paramètre | Rôle |
|---|---|
| `texte` | Texte à dire à voix haute (600 caractères au plus, réglage `dire_max` de [`[voix]`](Configuration#voix)) |
| `nom_voix` | Voix à utiliser, proposée en autocomplétion (facultatif ; par défaut la voix de la campagne, voir [`/campagne voix`](#voix-de-la-campagne-campagne-voix)) |

Le bot rejoint **votre** canal vocal, dit le texte, puis le quitte. Permission « Gérer le serveur » ; la voix doit être
activée (`activer = oui`). La mise en forme (gras, titres, `[secret:…]`) est retirée avant la lecture.

Fonctionnement : le bot libère Whisper de la mémoire, synthétise (barre de progression), rejoue, puis recharge Whisper.
La voix est celle du paramètre `nom_voix`, sinon celle de la campagne active (`/campagne voix`), sinon la voix par défaut du moteur. **L'audio n'est jamais conservé** : il est généré dans un dossier temporaire,
supprimé dès la lecture terminée (les restes d'un arrêt brutal sont effacés au démarrage du bot). Redire le même texte le
resynthétise. Un ancien dossier `campagnes/<campagne>/dires/` peut être supprimé : le bot ne s'en sert plus.

**Pendant un enregistrement** : autorisée si `liberer_whisper = non` (défaut) : le bot parle dans le canal de la séance et sa
voix n'est pas transcrite. Avec `liberer_whisper = oui`, elle est refusée (la synthèse retire Whisper de la mémoire, la
transcription s'arrêterait) ; [`/campagne dicter`](#campagne) lit alors le « précédemment » déjà préparé.

### Voix de la campagne : `/campagne voix`

| Paramètre | Rôle |
|---|---|
| `nom_voix` | Voix à donner à la campagne, proposée en autocomplétion. Sans valeur : affiche la liste des voix et celle de la campagne |
| `nom` | Campagne concernée (défaut : la campagne active) |

Les voix sont des fichiers **`voix.<nom>.wav`** (ou `.mp3`, `.flac`, ~10 s de parole claire) dans le dossier commun `voix/`
(réglage `dossier` de [`[voix]`](Configuration#voix)), avec en option `voix.<nom>.txt` : ce que dit l'échantillon (utile à
OmniVoice). Ajouter un fichier suffit, sans redémarrer. `défaut` désigne la voix par défaut du moteur (sans échantillon).

Changer la voix demande « Gérer le serveur » ; le choix est enregistré dans `campagne.json` (`"voix": "<nom>"`). Ordre de
priorité : paramètre `nom_voix` d'une commande > voix de la campagne > ancien `voix.wav` du dossier de la campagne > réglage
`voix_reference` > voix par défaut du moteur. Les audios déjà préparés restent lisibles ; `/campagne audio force` les refait
avec la nouvelle voix. **N'utilisez que la voix d'une personne qui a donné son accord.**

## Règles de jeu

### `/regle`

| Paramètre | Rôle | Défaut |
|---|---|---|
| `jeu` | Jeu concerné | le jeu de la campagne en cours ; sans campagne, `jeu_par_defaut` de [`[regles]`](Configuration#regles) (Pathfinder première édition) |
| `phrases` | Nombre de dernières phrases à analyser (1 à 50) | `nb_phrases` (15) |
| `dicter` | `oui` : le bot dicte la réponse à voix haute (voix activée ; canal vocal de l'enregistrement, sinon le vôtre) | *(non)* |
| `nom_voix` | Voix de la dictée, proposée en autocomplétion ; sans effet sans `dicter` | la voix de la campagne en cours |
| `question` | Question libre envoyée à Gemini **à la place du transcript** (`phrases` est alors ignoré, 1500 caractères au plus, réglage `question_max`) | *(vide : le transcript est utilisé)* |

Envoie les dernières phrases du transcript à **Google Gemini**, qui explique ou précise la règle évoquée. Quand le transcript
est trop vague ou noyé, `question` envoie à la place un texte libre (consigne `instruction_question`) : aucun enregistrement
n'est alors nécessaire et rien du transcript n'est transmis. Nécessite une clé d'API Gemini (`GEMINI_API_KEY` ou `cle_api`).
Sans `question`, il faut un enregistrement en cours.

Avec `dicter`, la réponse écrite est publiée normalement puis lue ; les conditions sont vérifiées **avant** d'appeler Gemini
(voix activée, canal vocal à rejoindre, pas de conflit avec Whisper, voix existante, comme pour [`/dire`](#dire)). La lecture est
coupée à la dernière phrase entière avant `dire_max` caractères (600) ; un message privé le signale. Un délai minimal sépare deux appels sur un
même serveur (`delai_min_s`). Les phrases transmises sont envoyées à Google, avec le nom de leurs auteurs. La réponse
vient d'un modèle d'IA et d'une transcription automatique : à vérifier dans le livre de règles.

## Configuration

### `/config`

| Paramètre | Rôle |
|---|---|
| `cle` | Paramètre à consulter ou modifier (vide = tout afficher) |
| `valeur` | Nouvelle valeur : `oui` / `non`, un nombre, ou `lien` / `aucun` selon le paramètre |

Sans `valeur`, affiche la valeur actuelle (réponse privée). Avec `valeur`, la modifie dans `config.ini` et le publie dans
le salon. **Modifier demande la permission « Gérer le serveur ».** Seules les étapes de fin de session sont modifiables
ainsi : `mp3`, `mp3_kbps`, `rapport`, `drive`, `drive_audio`, `drive_transcript`, `drive_rapport`, `partage`.

## Campagnes

### `/campagne`

La commande unique de gestion des campagnes. Son paramètre `action` se choisit dans une liste ; les autres paramètres
dépendent de l'action. Les réponses sont privées, sauf `afficher`.

Une campagne est un dossier dans `campagnes/` ; la campagne **active** est la dernière choisie. Voir la page
[Accueil](Home) pour le déroulement complet.

| Paramètre | Rôle |
|---|---|
| `action` | L'action à effectuer (voir ci-dessous) |
| `nom` | Nom de la campagne (`nouvelle`, `choisir`) ; pour `info`, la campagne à décrire si ce n'est pas l'active |
| `jeu` | Jeu de la nouvelle campagne (défaut : `jeu_par_defaut`) |
| `mj` | MJ de la campagne (`nouvelle`, `mj`) |
| `membre` | Membre à ajouter comme joueur (`joueur`) |
| `personnage` | Personnage joué, de préférence décrit dans `contexte.md` (`joueur`) |
| `quoi` | Contenu à afficher : compte rendu, précédemment, résumé global (`afficher`) |
| `rubrique` | Une seule rubrique du résumé global, proposée en autocomplétion (`afficher`) |
| `minutes` | Durée d'une partie de pack en minutes, 0 = pas de découpe (`pack`) |
| `session` | Identifiant de séance, proposé en autocomplétion (`pack`, `integrer`, `resumer`, `afficher`, `dicter`, `audio`) |
| `fichier` | Fichier texte joint (`contexte`, `journal`, `pack`, `integrer` : facultatif, sinon presse-papiers, `resumer`) |
| `sortie` | `pack` : `presse-papiers` (défaut) ou `fichier` |
| `partie` | `pack` copié dans le presse-papiers : numéro de la partie à copier (défaut 1) |
| `nom_voix` | Nom d'une voix, proposé en autocomplétion : voix de l'audio préparé (`audio`, par défaut celle de la campagne), voix de la lecture à la volée (`dicter`) ou voix à donner à la campagne (`voix`) |

#### Actions de lecture (tout le monde)

| Action | Effet |
|---|---|
| `liste` | Liste les campagnes ; la campagne active est marquée ▶ |  |
| `info` | Détails de la campagne : MJ, joueurs, séances, versions du résumé global |  |
| `afficher` | Affiche le `quoi` voulu (compte rendu, « précédemment » ou résumé global) de la dernière séance ou de `session`. Avec `rubrique`, n'affiche que cette partie du résumé (fils ouverts, lieux, alliés…). Au-delà de 3 900 caractères, le texte est envoyé en fichier |  |
| `dicter` | Le bot lit à voix haute le « précédemment » dans votre canal vocal (ou dans l'enregistrement en cours). Sans `nom_voix` : lit l'audio préparé (voir `audio`). Avec `nom_voix` : génère le « précédemment » à la volée avec cette voix, lit, puis supprime l'audio ; l'audio préparé n'est ni lu ni remplacé (refusé pendant un enregistrement si `liberer_whisper = oui`). Sans enregistrement, le bot rejoint votre canal le temps de la lecture |  |

#### Actions de gestion (permission « Gérer le serveur »)

| Action | Paramètres | Effet |
|---|---|---|
| `nouvelle` | `nom`, `jeu`, `mj` | Crée la campagne et la rend active. Les membres du canal vocal deviennent joueurs ; le MJ n'est jamais déduit |
| `choisir` | `nom` | Change la campagne active (impossible pendant un enregistrement) |
| `mj` | `mj` | Désigne le MJ |
| `joueur` | `membre`, `personnage` | Ajoute ou met à jour un joueur, et l'associe à un personnage décrit dans `contexte.md` |
| `contexte` | `fichier` | Gemini structure votre texte (personnages, secrets, glossaire) en `contexte.md`. Aucun secret n'est déduit : sans indication, un fait est marqué `[à confirmer]`. Si `contexte.md` est déjà rempli, la proposition va dans `contexte_propose.md`. *Texte envoyé à Google* |
| `journal` | `fichier` | Importe un journal existant comme version 1 du résumé global |
| `pack` | `fichier`, `session`, `minutes`, `sortie`, `partie` | Prépare les prompts à coller dans un chat (Claude, Gemini…) et les copie dans le presse-papiers (ou en fichier avec `sortie:fichier`). Avec un transcript joint, `session` est obligatoire |
| `integrer` | `fichier` (facultatif) | Réinjecte la réponse du chat (`=== COMPTE RENDU ===`, `=== RESUME GLOBAL ===`, `=== PRECEDEMMENT ===`) après contrôle de lecture. Crée la version suivante du résumé global et prépare l'audio du « précédemment » |
| `resumer` | `fichier`, `session` | **Automatique** : pack → programme `claude` de ton abonnement → intégration (compte rendu, « précédemment », résumé global). `rollback` pour annuler. Réglages dans `[claude]` ; `moteur_resume = gemini` pour l'ancienne méthode. Sans fichier, reprend la dernière session enregistrée. *Texte envoyé à Anthropic* |
| `rollback` | | Annule la dernière version du résumé global **et** la séance associée (déplacée dans `seances/_annulees/`) |
| `audio` | `session`, `nom_voix` | Prépare l'audio du « précédemment ». Avec `liberer_whisper = oui`, Whisper est libéré de la mémoire pendant la synthèse puis rechargé, et la commande est refusée pendant un enregistrement |
| `audio force` | `session`, `nom_voix` | Refait l'audio même s'il est prêt (synthèse ratée, bruits). L'ancien est gardé en `precedemment.precedent.wav` |
| `voix` | `nom_voix`, `nom` | Sans `nom_voix` : liste les voix disponibles et celle de la campagne. Avec `nom_voix` : la choisit pour la campagne (*Gérer le serveur*), enregistrée dans `campagne.json`. Voir [Voix](#voix) |

Presque toutes les actions de gestion sont refusées pendant un enregistrement.

## Voir aussi

- [Configuration](Configuration) : toutes les clés de `config.ini` avec leurs valeurs par défaut.
- [Accueil](Home) : installation, déroulement d'une séance, structure des dossiers.
