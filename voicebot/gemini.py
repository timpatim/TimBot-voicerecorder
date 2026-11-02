"""Appel à Google Gemini, partagé par /regle et la gestion des campagnes.

Module indépendant de Discord : `generate(...)` renvoie le texte de la réponse ou lève `GeminiError` avec un message
lisible. Clé, modèle, niveau de réflexion, recherche web et délai viennent de la section [regles] de config.ini
(section historique : elle règle tout l'accès à Gemini).
"""
from .config import SET


class GeminiError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


def api_key() -> str:
    return SET["regles.cle_api"].strip()


def generate(system_instruction: str, contents: str, *, temperature: float | None = None,
             web_search: bool | None = None, timeout_s: int | None = None) -> str:
    """Appel bloquant (à lancer dans un thread). Renvoie la réponse en texte brut."""
    key = api_key()
    if not key:
        raise GeminiError("Aucune clé d'API Gemini : définis la variable d'environnement GEMINI_API_KEY "
                          "(ou `cle_api` dans la section [regles] de config.ini).")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise GeminiError("Bibliothèque manquante : `pip install google-genai`.") from None

    use_web = SET["regles.recherche_web"] if web_search is None else web_search
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_web else None
    level = SET["regles.reflexion"]
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=SET["regles.temperature"] if temperature is None else temperature,
        tools=tools,
        thinking_config=None if level == "auto" else types.ThinkingConfig(thinking_level=level),
        http_options=types.HttpOptions(timeout=(timeout_s or SET["regles.delai_attente_s"]) * 1000),
    )
    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(model=SET["regles.modele"], contents=contents, config=config)
        text = (response.text or "").strip()
    except Exception as e:                      # erreurs réseau, quota, clé invalide, modèle inconnu...
        msg = str(e)
        hint = ""
        if any(m in msg for m in ("DEADLINE_EXCEEDED", "504", "503", "UNAVAILABLE")):
            hint = ("\nGemini est lent ou surchargé : réessaie, ou dans [regles] de config.ini baisse `reflexion` "
                    "(low), désactive `recherche_web`, ou augmente `delai_attente_s`.")
        elif any(m in msg for m in ("thinking", "INVALID_ARGUMENT")):
            hint = "\nSi le modèle ne gère pas le niveau de réflexion, mets `reflexion = auto` dans [regles]."
        raise GeminiError(f"Gemini a renvoyé une erreur : {msg[:300]}{hint}") from e
    if not text:
        raise GeminiError("Gemini n'a renvoyé aucune réponse (contenu bloqué par ses filtres ?).")
    return text
