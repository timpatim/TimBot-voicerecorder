"""Synthèse vocale avec OmniVoice (k2-fsa), lancée par le bot dans un processus séparé.

Ce script tourne dans l'environnement conda dédié créé par install_omnivoice.bat (celui où `omnivoice` est installé),
pas dans celui du bot. Au démarrage il ne dépend que de la bibliothèque standard ; torch et omnivoice ne sont importés
que dans main().

Usage : python tools/tts_omnivoice.py --in texte.txt --out sortie.wav [--lang fr] [--voice echantillon.wav]
                                      [--voice-text "transcription"] [--speed 1.15] [--steps 32] [--device auto|cuda|cpu]

API utilisée (README d'OmniVoice) : OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=..., dtype=...) puis
model.generate(text=..., ref_audio=..., ref_text=..., speed=..., num_step=...) -> liste de tableaux à 24 kHz.
Le README ne documente pas le paramètre de langue : il n'est transmis que si la signature de generate() le propose.
"""
import argparse
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tts_chatterbox import split_text  # noqa: E402  (stdlib seulement)

MAX_CHARS = 250                  # morceaux courts : un long morceau risque d'être coupé avant sa fin
TAIL_S = 0.5                     # silence final : le dernier mot ne doit jamais être avalé
WORDS_PER_S = 3.6                # débit mesuré d'OmniVoice en français à vitesse 1.0 (mots / s)
MIN_RATIO = 0.6                  # en dessous de cette part de la durée attendue, un morceau est jugé tronqué
RETRIES = 2
SAMPLE_RATE = 24000
SPEED_APPLIED = "VITESSE_APPLIQUEE"      # annoncé au bot quand la vitesse a été réglée ici (il n'accélère pas ensuite)


def pick_params(accepted, args) -> tuple:
    """Paramètres de generate() à passer selon sa signature. -> (kwargs, notes). `accepted` : noms de paramètres, ou None
    si la fonction accepte **kwargs (tout est alors tenté)."""
    def ok(name):
        return accepted is None or name in accepted
    kwargs, notes = {}, []
    if abs(args.speed - 1.0) > 0.01:                 # par défaut, la vitesse est réglée par le bot (ffmpeg), pas par le modèle
        if ok("speed"):
            kwargs["speed"] = args.speed
        else:
            notes.append("vitesse non gérée par le moteur")
    if ok("num_step"):
        kwargs["num_step"] = args.steps
    for name in ("language", "language_id", "lang"):
        if ok(name) and accepted is not None:
            kwargs[name] = args.lang
            break
    else:
        notes.append("langue non transmise (détection automatique)")
    if args.voice:
        kwargs["ref_audio"] = args.voice
        if args.voice_text:
            kwargs["ref_text"] = args.voice_text
    return kwargs, notes


def expected_seconds(chunk: str, speed: float) -> float:
    return len(chunk.split()) / (WORDS_PER_S * max(speed, 0.1))


def is_truncated(seconds: float, chunk: str, speed: float) -> bool:
    """Un morceau nettement plus court que ce que son texte demande a probablement perdu sa fin."""
    return len(chunk.split()) >= 4 and seconds < MIN_RATIO * expected_seconds(chunk, speed)


def to_array(audio):
    """Premier résultat de generate() -> tableau numpy à une dimension (tenseur torch ou tableau numpy)."""
    import numpy as np
    wav = audio[0]
    wav = wav.detach().float().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav, dtype=np.float32)
    return wav.reshape(-1)


def synthesize_chunk(model, chunk, kwargs, speed, can_duration=True, log=print):
    """Génère un morceau, et le régénère s'il est tronqué.
    - Essai 1 : réglages normaux.
    - Essais suivants : durée imposée d'après le nombre de mots (la durée d'OmniVoice se prédit à partir du texte :
      refaire le même essai donnerait le même résultat).
    -> (tableau numpy, secondes)"""
    best, attempt = None, 0
    while attempt <= RETRIES:
        kw = dict(kwargs)
        if attempt > 0 and can_duration:
            kw["duration"] = round(expected_seconds(chunk, speed), 2)
        wav = to_array(model.generate(text=chunk, **kw))
        if best is None or len(wav) > len(best):
            best = wav
        seconds = len(best) / SAMPLE_RATE
        if not is_truncated(seconds, chunk, speed):
            break
        attempt += 1
        if attempt <= RETRIES:
            log(f"ALERTE : morceau trop court ({seconds:.1f} s pour environ {expected_seconds(chunk, speed):.1f} s attendues), "
                f"nouvel essai {attempt}/{RETRIES}" + (" avec une durée imposée" if can_duration else ""))
    return best, len(best) / SAMPLE_RATE


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="src", help="fichier texte à lire")
    ap.add_argument("--out", help="fichier WAV à écrire")
    ap.add_argument("--lang", default="fr")
    ap.add_argument("--voice", default="", help="échantillon audio (~10 s) de la voix à imiter ; vide = voix choisie par le modèle")
    ap.add_argument("--voice-text", default="", help="transcription de l'échantillon (sinon OmniVoice la devine avec Whisper)")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--steps", type=int, default=32)
    ap.add_argument("--pause", type=float, default=0.25, help="silence (s) entre deux morceaux")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--serve", action="store_true",
                    help="reste en marche : charge le modèle une fois, puis lit sur l'entrée standard une requête JSON par ligne "
                         '({"argv": [...]}) et répond « TERMINE <code> » après chacune')
    return ap


def load_model(device_arg: str):
    """Charge OmniVoice. -> (modèle, paramètres acceptés par generate() ou None si **kwargs)."""
    import torch
    from omnivoice import OmniVoice

    device = device_arg
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Chargement d'OmniVoice ({device})...", flush=True)
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice",
                                      device_map="cuda:0" if device == "cuda" else "cpu",
                                      dtype=torch.float16 if device == "cuda" else torch.float32)
    try:
        params = inspect.signature(model.generate).parameters
        accepted = None if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()) else set(params)
    except (TypeError, ValueError):
        accepted = None
    return model, accepted


def run_request(model, accepted, args) -> int:
    """Synthétise le texte de args.src dans args.out avec un modèle déjà chargé."""
    import numpy as np
    import soundfile as sf

    if not args.src or not args.out:
        print("Il faut --in et --out.", file=sys.stderr)
        return 2
    with open(args.src, encoding="utf-8") as f:
        chunks = split_text(f.read(), MAX_CHARS)
    if not chunks:
        print("Texte vide.", file=sys.stderr)
        return 2
    kwargs, notes = pick_params(accepted, args)
    for note in notes:
        print(f"NOTE : {note}", flush=True)
    silence = np.zeros(int(SAMPLE_RATE * args.pause), dtype=np.float32)
    can_duration = accepted is None or "duration" in accepted
    pieces = []
    for i, chunk in enumerate(chunks, 1):
        print(f"MORCEAU {i}/{len(chunks)}", flush=True)
        best, seconds = synthesize_chunk(model, chunk, kwargs, args.speed, can_duration, lambda m: print(m, flush=True))
        print(f"FAIT {i}/{len(chunks)} duree={seconds:.1f}s attendu={expected_seconds(chunk, args.speed):.1f}s", flush=True)
        pieces += [best, silence]
    pieces[-1] = np.zeros(int(SAMPLE_RATE * TAIL_S), dtype=np.float32)
    sf.write(args.out, np.concatenate(pieces), SAMPLE_RATE)
    if "speed" in kwargs:
        print(SPEED_APPLIED, flush=True)
    print("OK", flush=True)
    return 0


def serve(model, accepted, stdin=None, parser=None) -> None:
    """Boucle du mode permanent : une requête JSON par ligne, réponse « TERMINE <code> ». Une erreur ne l'arrête pas."""
    parser = parser or build_parser()
    print("PRET", flush=True)
    for line in (stdin or sys.stdin):
        line = line.strip()
        if not line:
            continue
        try:
            args = parser.parse_args(json.loads(line)["argv"])
            code = run_request(model, accepted, args)
        except SystemExit:                                       # argparse a refusé les paramètres
            code = 2
            print("ERREUR paramètres invalides", flush=True)
        except Exception as e:                                   # noqa: BLE001 - signalé au bot, le processus reste utilisable
            code = 1
            print(f"ERREUR {type(e).__name__}: {' '.join(str(e).split())[:300]}", flush=True)
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
        print(f"TERMINE {code}", flush=True)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.serve:
        model, accepted = load_model(args.device)
        serve(model, accepted)
        return 0
    if not args.src or not args.out:
        print("Il faut --in et --out (ou --serve).", file=sys.stderr)
        return 2
    with open(args.src, encoding="utf-8") as f:
        if not split_text(f.read(), MAX_CHARS):
            print("Texte vide.", file=sys.stderr)
            return 2
    model, accepted = load_model(args.device)
    return run_request(model, accepted, args)


if __name__ == "__main__":
    sys.exit(main())
