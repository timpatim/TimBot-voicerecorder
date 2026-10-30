"""Dépôt sur Google Drive (OAuth « application de bureau », scope drive.file)."""
import mimetypes
import threading
from pathlib import Path

from .config import SET


class DriveClient:
    """Google Drive via OAuth « application de bureau » (scope drive.file : le bot ne voit que ses propres fichiers).
    L'authentification se fait au démarrage, en arrière-plan : au tout premier lancement un navigateur
    s'ouvre pour autoriser l'accès, ensuite le jeton est réutilisé (token.json)."""

    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    STATE_TEXT = {"idle": "non démarré", "authenticating": "connexion…", "ready": "connecté",
                  "failed": "erreur", "not_configured": "non configuré"}

    def __init__(self):
        self.state = "idle"
        self.error = None
        self.service = None
        self.credentials_path = Path(SET["drive.identifiants"])
        self.token_path = Path(SET["drive.jeton"])
        self.root_name = SET["drive.dossier_racine"]
        self._done = threading.Event()
        self._lock = threading.Lock()              # le client Google n'est pas thread-safe

    @property
    def state_text(self) -> str:
        return self.STATE_TEXT[self.state]

    def start(self):
        if self.state != "idle":
            return
        self.state = "authenticating"
        threading.Thread(target=self._authenticate, daemon=True, name="drive-auth").start()

    def wait_ready(self, timeout: float | None = None) -> bool:
        self._done.wait(timeout)
        return self.state == "ready"

    def problem(self) -> str:
        if self.state == "not_configured":
            return f"{self.credentials_path} introuvable (voir la procédure Google Cloud)"
        if self.state == "failed":
            return f"connexion Google impossible : {self.error!r}"
        return "connexion Google non terminée (autorisation en attente dans le navigateur ?)"

    def _authenticate(self):
        try:
            if not self.token_path.exists() and not self.credentials_path.exists():
                self.state = "not_configured"
                print(f"[drive] {self.credentials_path} introuvable : dépôt Google Drive indisponible")
                return
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build

            creds = None
            if self.token_path.exists():
                creds = Credentials.from_authorized_user_file(str(self.token_path), self.SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception as e:
                        print(f"[drive] jeton expiré ({e!r}), nouvelle autorisation nécessaire")
                        creds = None
                else:
                    creds = None
                if creds is None:
                    print("[drive] autorisation requise : une page du navigateur va s'ouvrir…")
                    flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), self.SCOPES)
                    creds = flow.run_local_server(port=0)
                self.token_path.write_text(creds.to_json(), encoding="utf-8")
            self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
            self.state = "ready"
            print("[drive] connecté")
        except Exception as e:
            self.error = e
            self.state = "failed"
            print(f"[drive] connexion impossible : {e!r}")
        finally:
            self._done.set()

    @staticmethod
    def _ensure_folder(svc, name: str, parent: str | None) -> str:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"mimeType='application/vnd.google-apps.folder' and name='{safe}' and trashed=false"
        q += f" and '{parent or 'root'}' in parents"
        found = svc.files().list(q=q, spaces="drive", fields="files(id)", pageSize=1).execute(num_retries=3)
        if found.get("files"):
            return found["files"][0]["id"]
        body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            body["parents"] = [parent]
        return svc.files().create(body=body, fields="id").execute(num_retries=3)["id"]

    def upload_session(self, session_id: str, files: list, share: bool, progress=None) -> dict:
        """files : [(chemin, sous-dossier ou None)]. Crée <racine>/<session>/ et y dépose les fichiers.
        progress(i, n, nom, pourcentage). Retourne {link, shared, note, count}."""
        if self.state == "idle":
            self.start()
        if not self.wait_ready(timeout=SET["drive.attente_connexion_s"]):
            raise RuntimeError(self.problem())
        from googleapiclient.http import MediaFileUpload

        with self._lock:
            svc = self.service
            folder = self._ensure_folder(svc, session_id, self._ensure_folder(svc, self.root_name, None))
            subfolders: dict[str, str] = {}
            for i, (path, sub) in enumerate(files, 1):
                parent = folder
                if sub:
                    if sub not in subfolders:
                        subfolders[sub] = self._ensure_folder(svc, sub, folder)
                    parent = subfolders[sub]
                media = MediaFileUpload(str(path), resumable=True, chunksize=SET["drive.taille_bloc_mo"] * 1024 * 1024,
                                        mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                req = svc.files().create(body={"name": path.name, "parents": [parent]},
                                         media_body=media, fields="id")
                resp = None
                while resp is None:
                    status, resp = req.next_chunk(num_retries=3)
                    if progress:
                        progress(i, len(files), path.name, status.progress() * 100 if status else 100.0)
            shared, note = False, ""
            if share:
                try:
                    svc.permissions().create(fileId=folder, body={"type": "anyone", "role": "reader"},
                                             fields="id").execute(num_retries=3)
                    shared = True
                except Exception as e:             # ex. domaine Google Workspace qui interdit les liens publics
                    note = f"partage par lien impossible ({e})"
            link = svc.files().get(fileId=folder, fields="webViewLink").execute(num_retries=3)["webViewLink"]
        return {"link": link, "shared": shared, "note": note, "count": len(files)}
