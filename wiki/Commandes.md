# Commandes

Toutes les commandes sont des commandes slash, utilisables sur un serveur (pas en message privé). Les réponses marquées
« privé » ne sont visibles que par la personne qui lance la commande ; les autres sont publiées **dans le salon où la
commande est tapée**. Le salon `canal_messages` de [`[discord]`](Configuration#discord), s'il est renseigné, ne reçoit que
les messages système et informatifs : suivi de fin de session, alertes, **une ligne de trace à chaque commande lancée**
(`▶️ Nom : /commande paramètres`, sans le contenu des fichiers joints) et le compte rendu de `/campagne resumer` et
`/campagne condenser`. Les réponses de `/regle`, `/transcript`, `/stats` et `/campagne afficher` restent dans le salon de
la commande ; `/config` et `/campagne rollback` répondent en privé.

> Les commandes n'apparaissent parfois qu'après un rafraîchissement du client Discord (**Ctrl+R**). Pour une
> synchronisation instantanée, renseignez `guild_id` dans `[discord]`.

| Commande | Rôle | Permission |
|---|---|---|
| [`/record`](#record) | Démarre l'enregistrement du canal vocal | tous |
| [`/stop`](#stop) | Arrête et lance le traitement de fin de session | tous |
| [`/status`](#status) | État de l'enregistrement, de la file de transcription et de Drive | tous |
| [`/stats`](#stats) | Statistiques de la session en cours | tous |
| [`/transcript`](#transcript) | Derniers messages transcrits | tous |
| [`/retranscrire`](#retranscrire) | Refait le transcript d'une session depuis ses fichiers audio | *Gérer le serveur* |
| [`/dicter`](#dicter) | Le bot dit un texte à voix haute dans votre canal vocal | *Gérer le serveur* |
| [`/regle`](#regle) | Explication d'une règle de jeu par Gemini | tous |
| [`/config`](#config) | Affiche ou modifie les étapes de fin de session | modifier : *Gérer le serveur* |
| [`/campagne`](#campagne) | Campagnes de jeu de rôle : mémoire, résumés, voix (sous-commandes) | voir le tableau |
| [`/glossaire`](#glossaire) | Glossaire appris de la campagne : afficher, corriger, annuler | corriger, annuler : *Gérer le serveur* |

## Arbre des commandes

`/campagne` et `/glossaire` sont des **groupes de sous-commandes** : Discord ne propose que les paramètres de la
sous-commande choisie.

Légende : `*` obligatoire · `[ ]` facultatif · `🔒` demande la permission « Gérer le serveur ».

```
/record
/stop
/status
/stats
/transcript  [nombre]
/retranscrire 🔒  [session]
/dicter      🔒   texte*  [nom_voix]
/regle       [jeu]  [phrases]  [question]  [dicter [nom_voix]]
/config      [cle [valeur]]            🔒 pour modifier une valeur

/campagne
├─ liste
├─ info             [nom]
├─ nouvelle    🔒   nom*  [jeu]  [mj]
├─ choisir     🔒   nom*
├─ mj          🔒   mj*  [nom]
├─ joueur      🔒   membre*  [personnage]  [nom]
├─ contexte    🔒   [fichier]  [nom]
├─ journal     🔒   fichier*  [nom]
├─ afficher         [quoi]  [session]  [rubrique]  [nom]
├─ resumer     🔒   [fichier + session*]  [nom]
├─ condenser   🔒   [de + a*]  [titre]  [niveau]  [nom]
├─ rollback    🔒   [nom]
├─ audio       🔒   [session]  [nom_voix]  [forcer]  [nom]
├─ dicter           [session]  [nom_voix]  [nom]
└─ voix             [nom_voix]  [nom]        🔒 pour changer la voix (sans `nom_voix` : liste)

/glossaire
├─ afficher         [nom]
├─ corriger    🔒   precision*  [nom]
└─ annuler     🔒   [nom]
```

Précisions :

- **`nom`** désigne la campagne visée (le début du nom suffit). Sans `nom`, c'est la campagne active. Seules `nouvelle` et
  `choisir` l'exigent.
- **`afficher`** : `quoi` vaut *compte rendu* par défaut, ou *résumé global* si `rubrique` est donnée. `rubrique` ne
  s'applique qu'au résumé global, et `session` seulement au compte rendu et au « précédemment ».
- **`resumer`** : sans `fichier`, le bot prend la dernière session enregistrée (ou `session`). Avec un `fichier` joint
  (un transcript existant), `session` devient **obligatoire** (`2026-10-24` ou `2026-10-24_190000`) : c'est la date de la séance.
- **`condenser`** : `de` et `a` vont ensemble ; sans eux, Claude propose les regroupements.
- **`audio`** : refusée pendant un enregistrement seulement si `liberer_whisper = oui`.

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
5. envoi sur Google Drive et lien de partage.

La mémoire de campagne (compte rendu, résumé global, « précédemment ») ne fait pas partie de ces étapes : elle se lance à
la demande avec [`/campagne resumer`](#campagne).

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

### `/dicter`

| Paramètre | Rôle |
|---|---|
| `texte` | Texte à dire à voix haute (600 caractères au plus, réglage `dire_max` de [`[voix]`](Configuration#voix)) |
| `nom_voix` | Voix à utiliser, proposée en autocomplétion (facultatif ; par défaut la voix de la campagne, voir [`/campagne voix`](#voix-de-la-campagne-campagne-voix)) |

Le bot rejoint **votre** canal vocal, dit le texte, puis le quitte. Permission « Gérer le serveur » ; la voix doit être
activée (`activer = oui`). La mise en forme (gras, titres, `[secret:…]`) est retirée avant la lecture.

La voix est celle du paramètre `nom_voix`, sinon celle de la campagne active (`/campagne voix`), sinon la voix par défaut
globale. **L'audio n'est pas conservé sur la machine du bot** : il est généré dans un dossier temporaire, supprimé dès la
lecture terminée (les restes d'un arrêt brutal sont effacés au démarrage du bot). En revanche il vous est **envoyé en pièce
jointe dans un message privé** « ⬇️ Audio à télécharger » (MP3 si ffmpeg est installé, sinon WAV) ; au-delà de 10 Mo environ,
ou si Discord refuse l'envoi, un message en donne la raison. Redire le même texte le resynthétise.

**Pendant un enregistrement** : autorisée si `liberer_whisper = non` (défaut) : le bot parle dans le canal de la séance et sa
voix n'est pas transcrite. Avec `liberer_whisper = oui`, elle est refusée (la synthèse retire Whisper de la mémoire, la
transcription s'arrêterait) ; [`/campagne dicter`](#campagne) lit alors le « précédemment » déjà préparé.

Le même verbe sert partout : `/dicter` (un texte libre), `/campagne dicter` (le « précédemment ») et l'option `dicter` de
`/regle` (la réponse).

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
`voix_reference` (voix par défaut globale) > voix par défaut d'OmniVoice. Les audios déjà préparés restent lisibles ; `/campagne audio forcer:oui` les refait
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

Avec `dicter`, la réponse écrite est publiée normalement puis lue ; l'audio est joint à un message privé (MP3 si ffmpeg est installé, sinon WAV, 10 Mo au plus) pour le télécharger ; les conditions sont vérifiées **avant** d'appeler Gemini
(voix activée, canal vocal à rejoindre, pas de conflit avec Whisper, voix existante, comme pour [`/dicter`](#dicter)). La lecture est
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

Un groupe de sous-commandes. Une campagne est un dossier dans `campagnes/` ; la campagne **active** est la dernière
choisie. Les réponses sont privées, sauf `afficher`. Voir la page [Accueil](Home) pour le déroulement complet.

#### Sous-commandes de lecture (tout le monde)

| Sous-commande | Paramètres | Effet |
|---|---|---|
| `liste` | | Liste les campagnes ; la campagne active est marquée ▶, avec son nombre de séances intégrées et de condensés |
| `info` | `nom` | Détails de la campagne : MJ, joueurs, séances, résumé global |
| `afficher` | `quoi`, `session`, `rubrique`, `nom` | Affiche le compte rendu, le « précédemment » ou le résumé global, pour la dernière séance ou pour `session`. Avec `rubrique`, n'affiche que cette partie du résumé (fils ouverts, lieux, alliés…). Au-delà de 3 900 caractères, le texte est envoyé en fichier. La « dernière séance » peut être un condensé (voir `condenser`) |
| `dicter` | `session`, `nom_voix`, `nom` | Le bot lit à voix haute le « précédemment » dans votre canal vocal (ou dans l'enregistrement en cours). Sans `nom_voix` : lit l'audio préparé (voir `audio`). Avec `nom_voix` : le génère à la volée avec cette voix, sans toucher à l'audio préparé (refusé pendant un enregistrement si `liberer_whisper = oui`). L'audio vous est aussi envoyé en pièce jointe dans un message privé ; `precedemment.wav` n'est jamais supprimé |
| `voix` | `nom_voix`, `nom` | Sans `nom_voix` : liste les voix disponibles et celle de la campagne. Avec `nom_voix` : la choisit pour la campagne (*Gérer le serveur*). Voir [Voix](#voix) |

#### Sous-commandes de gestion (permission « Gérer le serveur »)

| Sous-commande | Paramètres | Effet |
|---|---|---|
| `nouvelle` | `nom`, `jeu`, `mj` | Crée la campagne et la rend active. Les membres du canal vocal deviennent joueurs ; le MJ n'est jamais déduit |
| `choisir` | `nom` | Change la campagne active (impossible pendant un enregistrement) |
| `mj` | `mj`, `nom` | Désigne le MJ |
| `joueur` | `membre`, `personnage`, `nom` | Ajoute ou met à jour un joueur, et l'associe à un personnage décrit dans `contexte.md` |
| `contexte` | `fichier`, `nom` | Claude structure votre texte (personnages, secrets, glossaire) en `contexte.md`. Aucun secret n'est déduit : sans indication, un fait est marqué `[à confirmer]`. Si `contexte.md` est déjà rempli, la proposition va dans `contexte_propose.md`. Sans `fichier`, le bot relit `contexte_brut.txt` du dossier de la campagne. *Texte envoyé à Anthropic (votre abonnement Claude)* |
| `journal` | `fichier`, `nom` | Importe un journal existant comme version 1 du résumé global |
| `resumer` | `fichier`, `session`, `nom` | Résume une séance en trois appels à `claude` (votre abonnement) : nettoyage du transcript (corrections validées par le bot) → analyse et questions de scénario aux joueurs (réponses avec un bouton de validation, réservées à *Gérer le serveur*, jamais bloquantes) → version finale intégrée, glossaire mis à jour, audio du « précédemment » préparé. Le modèle ne renvoie que ce qui change : les anciennes séances ne sont jamais réécrites. **Informations « à retenir »** : le modèle repère lui-même les demandes orales de retenir une information (« retiens bien ce que vient d'expliquer le MJ ») et la fait figurer dans une rubrique « À retenir » du compte rendu, dans la chronologie et dans les rubriques concernées. Avec un transcript joint, `session` est obligatoire. Réglages dans [`[claude]`](Configuration#claude) (`nettoyage`, `questions`…). *Texte envoyé à Anthropic* |
| `condenser` | `de`, `a`, `titre`, `niveau`, `nom` | Condense la « Chronologie » du résumé global : des blocs consécutifs (des séances, puis des chapitres) sont remplacés par un seul bloc *Chapitre* ou *Livre* qui garde l'essentiel, avec une section **Suite** quand la dernière séance ouvre un nouvel arc. Sans `de`/`a`, Claude propose les regroupements ; un aperçu est affiché avant validation. **Chaque condensé devient aussi une séance** : un dossier `seances/<dernière séance couverte>_chapitre_<titre>/` avec son compte rendu, son « précédemment » et son audio. Les comptes rendus des séances jouées ne sont jamais touchés. *Texte envoyé à Anthropic* |
| `rollback` | `nom` | Annule la dernière version du résumé global **et** la séance ou les condensés associés (déplacés dans `seances/_annulees/`) |
| `audio` | `session`, `nom_voix`, `forcer`, `nom` | Prépare l'audio du « précédemment ». `forcer:oui` le refait même s'il est prêt (synthèse ratée, autre voix) ; l'ancien est gardé en `precedemment.precedent.wav`. Avec `liberer_whisper = oui`, Whisper est libéré pendant la synthèse et la commande est refusée pendant un enregistrement |

Presque toutes les sous-commandes de gestion sont refusées pendant un enregistrement.

**Quel « précédemment » est lu ?** Toujours celui de la **dernière séance de la campagne, réelle ou condensée** (`/record`,
`afficher`, `dicter`). Si un condensé couvre la dernière séance, c'est le sien ; s'il existe des séances plus récentes, c'est
celui de la plus récente ; une nouvelle séance résumée reprend la main automatiquement. Un condensé dont les blocs n'ont pas
de date est rangé d'après sa position dans la chronologie : il ne passe jamais devant des séances qu'il ne couvre pas.

**Reprendre une campagne déjà commencée** : `contexte` (personnages et joueurs), `journal` (le journal existant) puis, pour
chaque transcript existant, `resumer fichier:… session:<date>`.

### `/glossaire`

| Sous-commande | Paramètres | Effet |
|---|---|---|
| `afficher` | `nom` | Affiche le glossaire appris (entrées numérotées). Ouvert à tous |
| `corriger` | `precision`, `nom` | Une information du glossaire était fausse : `precision` dit ce qui était faux et la bonne information, en une ou deux phrases. *Gérer le serveur* |
| `annuler` | `nom` | Restaure le glossaire d'avant la dernière correction. *Gérer le serveur* |

Le glossaire (`glossaire.md` dans le dossier de la campagne, modifiable à la main) garde les noms corrigés et les décisions
prises en répondant aux questions de `/campagne resumer`. Quand une de ces informations s'avère fausse (un personnage
confondu avec un autre…), `/glossaire corriger precision:"Le chef du fort n'est pas Lucien, c'est Thrune"` demande à Claude
les **opérations** à faire (supprimer, remplacer, ajouter), jamais un glossaire réécrit. Un **aperçu** s'affiche avec les
boutons *Appliquer* / *Annuler* : rien n'est modifié avant le clic. L'ancien glossaire est sauvegardé dans
`glossaire_versions/` et la précision est ajoutée comme décision datée.

Seules les analyses à venir en profitent : pour refaire la dernière séance, `/campagne rollback` puis `/campagne resumer`.

## Voir aussi

- [Configuration](Configuration) : toutes les clés de `config.ini` avec leurs valeurs par défaut.
- [Accueil](Home) : installation, déroulement d'une séance, structure des dossiers.
