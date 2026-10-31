"""Questions de règles : envoie les dernières phrases du transcript à Google Gemini.

Module indépendant de Discord : `explain_rule(lines, jeu)` renvoie le texte de la réponse ou lève `RulesError`
avec un message lisible. Les réglages (clé, modèle, consigne...) viennent de la section [regles] de config.ini.
"""
from .config import SET


class RulesError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def api_key() -> str:
    return SET["regles.cle_api"].strip()


def transcript_lines(rows: list, n: int) -> list:
    """Les n dernières phrases (ordre chronologique), au format « Nom : texte »."""
    rows = [r for r in sorted(rows, key=lambda r: r.get("start", 0.0)) if str(r.get("text", "")).strip()]
    return [f"{r.get('name', '?')} : {str(r['text']).strip()}" for r in rows[-n:]]


def build_instruction(jeu: str) -> str:
    return SET["regles.instruction"].replace("{jeu}", jeu)


def build_contents(lines: list, jeu: str) -> str:
    return (f"Jeu : {jeu}\n\nDernières phrases prononcées à la table (la plus récente en dernier) :\n"
            + "\n".join(lines))


def explain_rule(lines: list, jeu: str) -> str:
    """Appel bloquant à Gemini (à lancer dans un thread). Renvoie la réponse en texte brut."""
    key = api_key()
    if not key:
        raise RulesError("Aucune clé d'API Gemini : définis la variable d'environnement GEMINI_API_KEY "
                         "(ou `cle_api` dans la section [regles] de config.ini).")
    if not lines:
        raise RulesError("Aucune phrase transcrite à analyser pour l'instant.")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RulesError("Bibliothèque manquante : `pip install google-genai`.") from None

    tools = [types.Tool(google_search=types.GoogleSearch())] if SET["regles.recherche_web"] else None
    level = SET["regles.reflexion"]
    config = types.GenerateContentConfig(
        system_instruction=build_instruction(jeu),
        temperature=SET["regles.temperature"],
        tools=tools,
        thinking_config=None if level == "auto" else types.ThinkingConfig(thinking_level=level),
        http_options=types.HttpOptions(timeout=SET["regles.delai_attente_s"] * 1000),
    )
    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=SET["regles.modele"], contents=build_contents(lines, jeu), config=config)
        text = (response.text or "").strip()
    except Exception as e:                      # erreurs réseau, quota, clé invalide, modèle inconnu...
        msg = str(e)
        hint = ""
        if any(m in msg for m in ("DEADLINE_EXCEEDED", "504", "503", "UNAVAILABLE")):
            hint = ("\nGemini est lent ou surchargé : réessaie, ou dans [regles] de config.ini baisse `reflexion` "
                    "(low), désactive `recherche_web`, ou augmente `delai_attente_s`.")
        elif any(m in msg for m in ("thinking", "INVALID_ARGUMENT")):
            hint = "\nSi le modèle ne gère pas le niveau de réflexion, mets `reflexion = auto` dans [regles]."
        raise RulesError(f"Gemini a renvoyé une erreur : {msg[:300]}{hint}") from e
    if not text:
        raise RulesError("Gemini n'a renvoyé aucune réponse (contenu bloqué par ses filtres ?).")
    return text
