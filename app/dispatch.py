"""Dispatch des commandes vocales vers Hermes + réponses Telegram."""
import asyncio
import logging

import httpx

from .config import CONFIG, TELEGRAM_BOT_TOKEN

log = logging.getLogger("omi-bridge.dispatch")

# Anti-burst : limite le nombre de `hermes chat` simultanés (règle OA : max 5)
_hermes_semaphore = asyncio.Semaphore(CONFIG["limits"]["max_concurrent_hermes"])

PROMPT_TEMPLATE = """Tu reçois une demande vocale d'Alex, captée par son wearable OMI (commande "{keyword_label}").
Ta réponse sera envoyée telle quelle sur Telegram à Alex : réponds en français, de façon concise et directe (pas de markdown lourd, pas de préambule).
Si la demande nécessite une action (créer une carte kanban, vérifier un service, noter quelque chose), fais-la avec tes outils puis confirme en une phrase.
La transcription vocale peut contenir des erreurs : interprète avec bon sens.

DEMANDE D'ALEX :
{command}

CONTEXTE (ce qui se disait juste avant, même conversation) :
{context}"""


async def run_hermes(command: str, context: str, trigger: dict, timeout: int) -> str:
    """Lance `hermes chat -Q` et retourne la réponse finale (stdout)."""
    prompt = PROMPT_TEMPLATE.format(
        keyword_label=trigger.get("label", "Allo Omar"),
        command=command or "(rien après le mot-clé — réagis au contexte ci-dessous)",
        context=context or "(pas de contexte disponible)",
    )
    # hermes_extra_args (config) permet de cibler un autre agent/modèle,
    # ex. ["-m", "anthropic/claude-sonnet-4"] — par défaut : profil hermes courant (h-omar)
    args = ["hermes", "chat", "-Q", "-q", prompt] + list(trigger.get("hermes_extra_args", []))

    async with _hermes_semaphore:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"hermes chat timeout ({timeout}s)")
    if proc.returncode != 0:
        raise RuntimeError(f"hermes chat exit {proc.returncode}: {stderr.decode()[-300:]}")
    return stdout.decode().strip()


async def send_telegram(chat_id: int, text: str) -> None:
    """Envoie un message Telegram (découpé si > 4096 chars)."""
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN absent — message non envoyé")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["(réponse vide)"]
    async with httpx.AsyncClient(timeout=30) as client:
        for chunk in chunks:
            resp = await client.post(url, data={"chat_id": chat_id, "text": chunk})
            body = resp.json()
            if not body.get("ok"):
                log.error("Échec envoi Telegram: %s", body)
