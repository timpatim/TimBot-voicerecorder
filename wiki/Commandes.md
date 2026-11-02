# Commandes

Toutes les commandes sont des commandes slash, utilisables sur un serveur (pas en message privé). Les réponses marquées
« privé » ne sont visibles que par la personne qui lance la commande ; les autres sont publiées dans le salon texte de
la commande, ou dans le salon dédié si `canal_messages` est renseigné dans [`[discord]`](Configuration#discord).

> Les commandes n'apparaissent parfois qu'après un rafraîchissement du client Discord (**Ctrl+R**). Pour une
> synchronisation instantanée, renseignez `guild_id` dans `[discord]`.

| Commande | Rôle | Permission |
|---|---|---|
| [`/record`](#record) | Démarre l'enregistrement du canal vocal | tous |
| [`/stop`](#stop) | Arrête et lance le traitement de fin de session | tous |
| [`/status`](#status) | État de l'enregistrement, de la file de transcription et de Drive | tous |
| [`/stats`](#stats) | Statistiques de la session en cours | tous |
| [`/transcript`](#transcript) | Derniers messages transcrits | tous |
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
/transcript  [nombre]
/regle       [jeu]  [phrases]
/config      [cle [valeur]]            🔒 pour modifier une valeur

/campagne  action:
├─ liste
├─ info             [nom]
├─ afficher         [nom]  [quoi]  [session]  [rubrique]
├─ dicter           [nom]  [session]
├─ nouvelle    🔒   nom*  [jeu]  [mj]
├─ choisir     🔒   nom*
├─ mj          🔒   mj*  [nom]
├─ joueur      🔒   membre*  [personnage]  [nom]
├─ contexte    🔒   fichier*  [nom]
├─ journal     🔒   fichier*  [nom]
├─ pack        🔒   [fichier + session*]  [minutes]  [nom]
├─ integrer    🔒   fichier*  [session]  [nom]
├─ resumer     🔒   [fichier + session*]  [nom]
├─ rollback    🔒   [nom]
├─ audio       🔒   [session]  [nom]
└─ audio force 🔒   [session]  [nom]
```

Précisions :

- **`nom`** désigne la campagne visée. Sans `nom`, c'est la campagne active. Seules `nouvelle` et `choisir` l'exigent.
- **`afficher`** : `quoi` vaut *compte rendu* par défaut, ou *résumé global* si `rubrique` est donnée. `rubrique` ne
  s'applique qu'au résumé global, et `session` seulement au compte rendu et au « précédemment ».
- **`pack`** et **`resumer`** : sans `fichier`, le bot prend la dernière session enregistrée (ou `session`). Avec un
  `fichier` joint, `session` devient **obligatoire** (`2026-10-24` ou `2026-10-24_190000`) : c'est la date de la séance.
- **`integrer`** : `session` sert à choisir la séance à laquelle rattacher la réponse ; par défaut, la séance en attente.
- **`audio`** et **`audio force`** : refusées pendant un enregistrement.
- **`liste`** n'utilise aucun paramètre.

### Tableau croisé

| Action | `nom` | `jeu` | `mj` | `membre` | `personnage` | `quoi` | `rubrique` | `minutes` | `session` | `fichier` |
|---|---|---|---|---|---|---|---|---|---|---|
| `liste` | | | | | | | | | | |
| `info` | ○ | | | | | | | | | |
| `afficher` | ○ | | | | | ○ | ○ | | ○ | |
| `dicter` | ○ | | | | | | | | ○ | |
| `nouvelle` | ● | ○ | ○ | | | | | | | |
| `choisir` | ● | | | | | | | | | |
| `mj` | ○ | | ● | | | | | | | |
| `joueur` | ○ | | | ● | ○ | | | | | |
| `contexte` | ○ | | | | | | | | | ● |
| `journal` | ○ | | | | | | | | | ● |
| `pack` | ○ | | | | | | | ○ | ◐ | ○ |
| `integrer` | ○ | | | | | | | | ○ | ● |
| `resumer` | ○ | | | | | | | | ◐ | ○ |
| `rollback` | ○ | | | | | | | | | |
| `audio` | ○ | | | | | | | | ○ | |
| `audio force` | ○ | | | | | | | | ○ | |

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
5. mémoire de campagne (préparation des packs, ou résumé par API si activé),
6. envoi sur Google Drive et lien de partage.

Les étapes MP3, rapport, Drive et partage se règlent dans [`[fin_de_session]`](Configuration#fin_de_session) ou avec
[`/config`](#config) ; ce réglage est relu à chaque `/stop`. Refusée s'il n'y a pas d'enregistrement en cours.

### `/status`

Réponse privée, pendant un enregistrement : identifiant et durée de la session, nombre de participants, état de la file
de transcription (une file qui s'allonge signale un retard de WhisperX) et état de Google Drive.

### `/stats`

Statistiques de la session en cours (temps de parole par participant, palmarès), dans un encadré. Nécessite un
enregistrement en cours. Le contenu se règle dans [`[stats]`](Configuration#stats).

### `/transcript`

| Paramètre | Rôle | Défaut |
|---|---|---|
| `nombre` | Combien de messages afficher (1 à 50) | 10 |

Affiche les derniers messages transcrits, visibles de tous (nécessite un enregistrement en cours). Aucune transcription n'est publiée automatiquement : cette
commande est le seul moyen de les voir pendant la session.

## Règles de jeu

### `/regle`

| Paramètre | Rôle | Défaut |
|---|---|---|
| `jeu` | Jeu concerné | le jeu de la campagne en cours ; sans campagne, `jeu_par_defaut` de [`[regles]`](Configuration#regles) (Pathfinder première édition) |
| `phrases` | Nombre de dernières phrases à analyser (1 à 50) | `nb_phrases` (15) |

Envoie les dernières phrases du transcript à **Google Gemini**, qui explique ou précise la règle évoquée. Nécessite une
clé d'API Gemini (`GEMINI_API_KEY` ou `cle_api`) et un enregistrement en cours. Un délai minimal sépare deux appels sur un
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
| `fichier` | Fichier texte joint (`contexte`, `journal`, `pack`, `integrer`, `resumer`) |

#### Actions de lecture (tout le monde)

| Action | Effet |
|---|---|
| `liste` | Liste les campagnes ; la campagne active est marquée ▶ |
| `info` | Détails de la campagne : MJ, joueurs, séances, versions du résumé global |
| `afficher` | Affiche le `quoi` voulu (compte rendu, « précédemment » ou résumé global) de la dernière séance ou de `session`. Avec `rubrique`, n'affiche que cette partie du résumé (fils ouverts, lieux, alliés…). Au-delà de 3 900 caractères, le texte est envoyé en fichier |
| `dicter` | Le bot lit à voix haute le « précédemment » dans votre canal vocal (ou dans l'enregistrement en cours). Nécessite l'audio préparé (voir `audio`) ; sans enregistrement, le bot rejoint votre canal le temps de la lecture |

#### Actions de gestion (permission « Gérer le serveur »)

| Action | Paramètres | Effet |
|---|---|---|
| `nouvelle` | `nom`, `jeu`, `mj` | Crée la campagne et la rend active. Les membres du canal vocal deviennent joueurs ; le MJ n'est jamais déduit |
| `choisir` | `nom` | Change la campagne active (impossible pendant un enregistrement) |
| `mj` | `mj` | Désigne le MJ |
| `joueur` | `membre`, `personnage` | Ajoute ou met à jour un joueur, et l'associe à un personnage décrit dans `contexte.md` |
| `contexte` | `fichier` | Gemini structure votre texte (personnages, secrets, glossaire) en `contexte.md`. Aucun secret n'est déduit : sans indication, un fait est marqué `[à confirmer]`. Si `contexte.md` est déjà rempli, la proposition va dans `contexte_propose.md`. *Texte envoyé à Google* |
| `journal` | `fichier` | Importe un journal existant comme version 1 du résumé global |
| `pack` | `fichier`, `session`, `minutes` | Prépare les prompts à coller dans un chat (Claude, Gemini…). Avec un transcript joint, `session` est obligatoire |
| `integrer` | `fichier` | Réinjecte la réponse du chat (`=== COMPTE RENDU ===`, `=== RESUME GLOBAL ===`, `=== PRECEDEMMENT ===`) après contrôle de lecture. Crée la version suivante du résumé global et prépare l'audio du « précédemment » |
| `resumer` | `fichier`, `session` | Variante par l'API Gemini (désactivée par défaut, `resume_auto`). Sans fichier, reprend la dernière session enregistrée |
| `rollback` | | Annule la dernière version du résumé global **et** la séance associée (déplacée dans `seances/_annulees/`) |
| `audio` | `session` | Prépare l'audio du « précédemment ». Refusée pendant un enregistrement : Whisper est libéré de la mémoire, Chatterbox synthétise, puis Whisper est rechargé |
| `audio force` | `session` | Refait l'audio même s'il est prêt (synthèse ratée, bruits). L'ancien est gardé en `precedemment.precedent.wav` |

Presque toutes les actions de gestion sont refusées pendant un enregistrement.

## Voir aussi

- [Configuration](Configuration) : toutes les clés de `config.ini` avec leurs valeurs par défaut.
- [Accueil](Home) : installation, déroulement d'une séance, structure des dossiers.
