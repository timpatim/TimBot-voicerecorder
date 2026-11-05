"""Rejoue une session enregistrée avec d'autres réglages de transcription et compare les résultats.

Les WAV par joueur de la session sont redécoupés puis passés à WhisperX pour chaque « variante » (jeu de réglages). Rien n'est
modifié dans la session : tout est écrit dans <session>/comparaison/.

Usage :
    python tools/comparer_transcription.py sessions/2026-11-03_214508 --de 00:40:00 --a 01:00:00
    python tools/comparer_transcription.py sessions/2026-11-03_214508 --sans-whisper          (découpage seul, rapide, sans GPU)
    python tools/comparer_transcription.py sessions/... --variantes actuel,filtre --variante "test=silence_fin_ms:900,fusion_s:1"
    python tools/comparer_transcription.py sessions/... --reference extrait_corrige.txt --de 00:40:00 --a 00:45:00

Variantes prédéfinies (chacune ajoute à la précédente) :
    actuel    réglages de ton config.ini
    reglages  silence_fin_ms 1100, min_trames_voisees 22, « au revoir / bye / amen » ignorés comme mots isolés
    filtre    reglages SANS ignorer ces mots, mais rejet de « au revoir / bye / amen / merci » sur un énoncé < 0,6 s
    fusion    filtre + fusion des énoncés d'un même joueur séparés de moins de 1,2 s
    contexte  fusion + fin de la phrase précédente du joueur donnée à Whisper comme contexte (selon la version de WhisperX)

--reference : un extrait du transcript corrigé à la main (même format que transcript_live.txt) ; le script calcule alors le taux
d'erreurs (WER) de chaque variante sur cet extrait (utiliser --de / --a sur la même plage).
"""
import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_CONFIG", str(ROOT / "config.ini"))     # lit TES réglages ; ne les modifie pas
sys.argv, _args = sys.argv[:1], sys.argv[1:]

from voicebot import rejeu                                       # noqa: E402


def hms(text: str) -> float:
    parts = [float(x) for x in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def main(argv=None, engine=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", type=Path, help="dossier de la session (contient audio/ et meta.json)")
    ap.add_argument("--variantes", default="actuel,reglages,filtre,fusion,contexte", help="liste séparée par des virgules")
    ap.add_argument("--variante", action="append", default=[], metavar="nom=cle:val,cle:val",
                    help="variante personnalisée (répétable), dérivée de « actuel »")
    ap.add_argument("--de", default=None, help="début de l'extrait (hh:mm:ss)")
    ap.add_argument("--a", default=None, help="fin de l'extrait (hh:mm:ss)")
    ap.add_argument("--joueurs", default="", help="noms de joueurs à traiter (séparés par des virgules ; défaut : tous)")
    ap.add_argument("--reference", type=Path, default=None, help="extrait corrigé à la main, pour calculer le WER")
    ap.add_argument("--sans-whisper", action="store_true", help="découpage seul : statistiques d'énoncés, sans transcrire")
    ap.add_argument("--sortie", type=Path, default=None, help="dossier de résultats (défaut : <session>/comparaison)")
    args = ap.parse_args(_args if argv is None else argv)

    session = args.session
    tracks = rejeu.session_tracks(session)
    wanted = {x.strip().lower() for x in args.joueurs.split(",") if x.strip()}
    if wanted:
        tracks = [t for t in tracks if t[1].lower() in wanted]
    if not tracks:
        print(f"Aucun fichier audio trouvé dans {session / 'audio'}.")
        return 1
    window = (hms(args.de) if args.de else 0.0, hms(args.a) if args.a else 10 ** 9) if (args.de or args.a) else None
    out = args.sortie or session / "comparaison"
    out.mkdir(parents=True, exist_ok=True)

    base = rejeu.from_config("actuel")
    available = rejeu.presets(base)
    chosen = {}
    for name in [n.strip() for n in args.variantes.split(",") if n.strip()]:
        if name not in available:
            print(f"Variante inconnue « {name} » (disponibles : {', '.join(available)}).")
            return 1
        chosen[name] = available[name]
    for spec in args.variante:
        p = rejeu.parse_variant(spec, base)
        chosen[p.nom] = p

    asr = None
    if not args.sans_whisper:
        if engine is None:
            from voicebot.whisper_engine import WhisperEngine
            engine = WhisperEngine()
            engine.start_loading()
            print("Chargement de WhisperX…")
            if not engine.wait_ready():
                print(f"WhisperX indisponible : {engine.error!r}")
                return 1
        asr = rejeu.AsrCache(out / "cache_whisper.jsonl")
    reference = rejeu.read_transcript(args.reference) if args.reference else None

    results, wers = {}, {}
    flags_cache, pcm_cache = {}, {}
    for name, params in chosen.items():
        t = time.monotonic()
        print(f"\n== Variante « {name} » ==")
        res = rejeu.transcribe_variant(params, tracks, None if args.sans_whisper else engine, window, asr,
                                       progress=lambda s: print("  ", s), flags_cache=flags_cache, pcm_cache=pcm_cache)
        results[name] = res
        if not args.sans_whisper:
            (out / f"transcript_{name}.txt").write_text(rejeu.transcript_text(res["rows"], f"{session.name} — variante {name}"),
                                                        encoding="utf-8")
            if reference:
                wers[name] = rejeu.wer(reference, res["rows"], window)
        print(f"   {res['enonces']} énoncé(s), {len(res['rows'])} ligne(s) gardée(s), {time.monotonic() - t:.0f} s")

    if args.sans_whisper:
        print("\nÉnoncés produits par le découpage (sans Whisper) :")
        for name, res in results.items():
            print(f"  {name:10s} {res['enonces']:6d} énoncés   écartés avant Whisper : {res['avant_whisper']}")
        return 0

    (out / "rapport.md").write_text(rejeu.report(results, wers or None), encoding="utf-8")
    names = list(results)
    for other in names[1:]:
        diff = rejeu.differences(results[names[0]]["rows"], results[other]["rows"])
        lines = [f"# {names[0]} ↔ {other} : fenêtres de 30 s où le texte diffère (les plus différentes d'abord)", ""]
        for start, a, b, n in diff:
            lines += [f"## {rejeu.fmt_ts(start)[:8]} ({n} mots d'écart)", f"**{names[0]}**", "```", a or "(rien)", "```",
                      f"**{other}**", "```", b or "(rien)", "```", ""]
        (out / f"differences_{names[0]}_{other}.md").write_text("\n".join(lines), encoding="utf-8")
    rej = []
    for name, res in results.items():
        rej += [f"[{rejeu.fmt_ts(r['start'])[:8]}] {name} · {r['name']} · {r['reason']} · {r['utt_s']} s : {r['text']}"
                for r in res["rejetes"]]
    (out / "rejetes.txt").write_text("\n".join(rej) + "\n", encoding="utf-8")
    print(f"\nRésultats dans {out} :\n  rapport.md · transcript_<variante>.txt · differences_*.md · rejetes.txt")
    print("\n" + (out / "rapport.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
