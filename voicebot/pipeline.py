"""Traitement de fin de session : étapes enchaînées et suivi dans le chat."""
import asyncio
import json
import shlex
import subprocess
import threading
import time

from .config import SET
from .drive import DriveClient
from .mp3 import convert_to_mp3
from .report import build_report
from .stats import render_text, session_stats
from .transcript import read_rows, write_final_transcript


class StatusBoard:
    """Un message unique, édité au fil des étapes (✅ ⏳ ❌ ⏭️). `update` peut être appelé depuis un thread."""

    ICONS = {"pending": "▫️", "running": "⏳", "ok": "✅", "failed": "❌", "skipped": "⏭️"}

    def __init__(self, channel, loop, title: str, steps: list, mentions=None):
        self.channel, self.loop, self.title, self.steps, self.mentions = channel, loop, title, steps, mentions
        self.state = {k: ("pending", "") for k, _ in steps}
        self.msg = None
        self._lock = threading.Lock()
        self._task = None
        self._again = False

    def render(self) -> str:
        with self._lock:
            lines = [self.title]
            for key, label in self.steps:
                st, detail = self.state[key]
                lines.append(f"{self.ICONS[st]} {label}" + (f" — {detail}" if detail else ""))
        return "\n".join(lines)[:1990]

    async def start(self):
        self.msg = await self.channel.send(self.render(), allowed_mentions=self.mentions)

    def update(self, key: str, state: str, detail: str = ""):
        with self._lock:
            self.state[key] = (state, detail)
        self.loop.call_soon_threadsafe(self._kick)

    def _kick(self):
        if self._task is None or self._task.done():
            self._task = self.loop.create_task(self._pump())
        else:
            self._again = True

    async def _pump(self):                         # au plus une édition par seconde (limites Discord)
        while True:
            self._again = False
            await self._edit()
            await asyncio.sleep(1.0)
            if not self._again:
                return

    async def _edit(self):
        if self.msg is None:
            return
        try:
            await self.msg.edit(content=self.render(), allowed_mentions=self.mentions)
        except Exception as e:
            print(f"[bot] mise à jour du suivi impossible : {e!r}")

    async def refresh(self):
        if self._task is not None and not self._task.done():
            await self._task
        await self._edit()


# --- le traitement lui-même ---

PIPELINE_STEPS = [("asr", "Fin de la transcription"), ("transcript", "Transcript et statistiques"),
                  ("mp3", "Conversion MP3"), ("rapport", "Rapport PDF"), ("drive", "Google Drive")]


def run_pipeline(rec: "SessionRecorder", live: "LiveTranscriber", cfg: dict, board: StatusBoard,
                 drive: DriveClient) -> dict:
    """Exécute les étapes l'une après l'autre. Une étape en échec n'empêche pas les suivantes."""
    res = {"stats": None, "transcript": None, "mp3": [], "report": None,
           "link": None, "shared": False, "notes": []}

    # 1. fin de la transcription : on laisse WhisperX vider sa file
    board.update("asr", "running")
    rec.stop()
    live.stop()
    deadline = time.monotonic() + SET["live.attente_fin_asr_s"]
    while live.is_alive() and time.monotonic() < deadline:
        live.join(timeout=2)
        if live.is_alive():
            board.update("asr", "running", f"{live.pending_seconds():.0f} s d'audio en attente")
    if live.is_alive():
        board.update("asr", "failed", "délai dépassé, transcript incomplet (retraitement conseillé)")
        res["notes"].append("La transcription live n'était pas terminée : relance ton retraitement sur la session.")
    elif live.dead:
        board.update("asr", "failed", "WhisperX indisponible, pas de transcription live")
    else:
        board.update("asr", "ok")

    # 2. transcript fusionné + statistiques (toujours)
    board.update("transcript", "running")
    try:
        res["transcript"] = write_final_transcript(rec)
        res["stats"] = stats = session_stats(rec)
        (rec.dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        (rec.dir / "stats.txt").write_text(render_text(stats), encoding="utf-8")
        cmd = SET["fin_de_session.commande_retraitement"]
        if cmd:
            subprocess.Popen(shlex.split(cmd) + [str(rec.dir)])
        board.update("transcript", "ok", f"{stats['lines']} lignes, {stats['words']} mots")
    except Exception as e:
        board.update("transcript", "failed", str(e)[:150])

    # 3. MP3
    if cfg["mp3"]:
        board.update("mp3", "running")
        t0 = time.monotonic()
        try:
            res["mp3"] = convert_to_mp3(rec, cfg["mp3_kbps"],
                                        lambda i, n, name: board.update("mp3", "running", f"{i}/{n} — {name}"))
            board.update("mp3", "ok", f"{len(res['mp3'])} fichier(s) en {time.monotonic() - t0:.0f} s")
        except Exception as e:
            board.update("mp3", "failed", str(e)[:150])

    # 4. rapport PDF
    if cfg["rapport"]:
        board.update("rapport", "running")
        try:
            if res["stats"] is None:
                raise RuntimeError("pas de statistiques disponibles")
            res["report"] = build_report(res["stats"], read_rows(rec.dir / "transcript_live.jsonl"), rec,
                                         rec.dir / "rapport.pdf")
            board.update("rapport", "ok", f"{res['report'].stat().st_size // 1024} Ko")
        except Exception as e:
            board.update("rapport", "failed", str(e)[:150])

    # 5. Google Drive
    if cfg["drive"]:
        files = []
        if cfg["drive_transcript"] and res["transcript"]:
            files.append((res["transcript"], None))
        if cfg["drive_rapport"] and res["report"]:
            files.append((res["report"], None))
        if cfg["drive_audio"]:
            audio = res["mp3"] if cfg["mp3"] else sorted((rec.dir / "audio").glob("*.wav"))
            files += [(p, "audio") for p in audio]
            if cfg["mp3"] and not res["mp3"]:
                res["notes"].append("Audio non déposé sur le Drive : la conversion MP3 a échoué.")
        if not files:
            board.update("drive", "skipped", "rien à déposer")
        else:
            board.update("drive", "running", f"0/{len(files)}")
            try:
                out = drive.upload_session(
                    rec.session_id, files, cfg["partage"] == "lien",
                    lambda i, n, name, pct: board.update("drive", "running", f"{i}/{n} — {name} ({pct:.0f} %)"))
                res["link"], res["shared"] = out["link"], out["shared"]
                if out["note"]:
                    res["notes"].append(out["note"])
                board.update("drive", "ok", f"{out['count']} fichier(s) déposé(s)")
            except Exception as e:
                board.update("drive", "failed", str(e)[:150])
    return res
