"""Dispatch des commandes vocales vers H-Omar via webhook Hermes + réponses Telegram."""
import asyncio
import hashlib
import hmac
import json
import logging

import httpx

from .config import CONFIG, HERMES_WEBHOOK_URL, HERMES_WEBHOOK_SECRET

log = logging.getLogger("omi-bridge.dispatch")

# Garde-fou anti-burst (même si le webhook est async côté Hermes)
_hermes_semaphore = asyncio.Semaphore(CONFIG["limits"]["max_concurrent_hermes"])

PROMPT_TEMPLATE = """Tu reçois une demande vocale d'Alex, captée par son wearable OMI (commande "{keyword_label}").
Ta réponse part telle quelle sur Telegram : réponds TOUJOURS par du texte en français, concis et direct (pas de markdown lourd, pas de préambule).

DEMANDE D'ALEX :
{command}

CONTEXTE AUDIO (ce qui se disait juste avant) :
{context}

DONNÉES LIVE — KANBAN (pré-fetché par omi-bridge, accès direct DB — utilise ces chiffres sans faire de fetch web) :
{kanban_snapshot}"""

RESUME_TEMPLATE = """Nouvelle demande vocale d'Alex via OMI (suite de notre conversation) :
{command}

Contexte audio récent :
{context}

DONNÉES LIVE — KANBAN :
{kanban_snapshot}"""

CONV_OPEN_TEMPLATE = """Alex ouvre un MODE CONVERSATION via son wearable OMI (mot-clé "{keyword_label} conversation").
Fonctionnement : il parle librement ; à chaque pause tu reçois ce qu'il vient de dire et tu réponds en écoute active.
Tes réponses partent sur Telegram : courtes (2 à 6 phrases), en français, sans markdown lourd.
Rôle : prendre note, reformuler brièvement ce que tu retiens, poser au plus UNE question utile,
et quand c'est pertinent proposer : continuer à écouter / explorer un point / préparer un plan d'action.
IMPORTANT : n'exécute AUCUNE action lourde (build, modification, création) tant qu'Alex n'a pas dit "c'est parti".
Accumule plutôt un plan d'action au fil de l'eau.

Premières paroles d'Alex :
{command}

Contexte audio juste avant :
{context}"""

CONV_TURN_TEMPLATE = """(mode conversation, suite) Alex vient de dire :
{command}"""

CONV_LAUNCH_TEMPLATE = """Alex vient de dire « {command} » : GO.
Exécute maintenant le plan d'action accumulé dans cette conversation, avec tes outils,
puis envoie un rapport concis de ce qui a été fait."""

CONV_END_TEMPLATE = """Alex clôt la conversation sans lancer d'exécution.
Envoie un récapitulatif structuré et bref : points notés, décisions, liste "à faire" pour plus tard."""


async def get_kanban_snapshot() -> str:
    """Fetch live Kanban state via CLI to inject into prompt — no web/Tailnet fetch needed."""
    async def _run(*args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
            return out.decode().strip()
        except Exception:
            return ""

    stats, ready, blocked = await asyncio.gather(
        _run("hermes", "kanban", "stats"),
        _run("hermes", "kanban", "list", "--status", "ready"),
        _run("hermes", "kanban", "list", "--status", "blocked"),
    )
    parts = []
    if stats:
        parts.append(stats)
    if ready:
        parts.append(f"Ready:\n{ready}")
    if blocked:
        # Limite à 8 lignes pour ne pas surcharger le prompt
        blocked_lines = blocked.splitlines()[:8]
        parts.append(f"Bloquées (top 8):\n" + "\n".join(blocked_lines))
    return "\n\n".join(parts) if parts else "(kanban indisponible)"


def _sign(secret: str, body: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


async def post_to_webhook(prompt: str) -> None:
    """Envoie le prompt formaté à H-Omar via le webhook Hermes.

    H-Omar reçoit le message avec son contexte complet (Kanban, mémoire, outils)
    et répond directement sur Telegram. Pas de valeur de retour : la réponse
    arrive en asynchrone depuis H-Omar.
    """
    if not HERMES_WEBHOOK_URL or not HERMES_WEBHOOK_SECRET:
        raise RuntimeError("HERMES_WEBHOOK_URL / HERMES_WEBHOOK_SECRET manquants (.env)")

    body = json.dumps({"prompt": prompt})
    sig = _sign(HERMES_WEBHOOK_SECRET, body)

    async with _hermes_semaphore:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                HERMES_WEBHOOK_URL,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                },
            )
            resp.raise_for_status()
    log.info("webhook OMI → H-Omar : %s", resp.status_code)


async def run_hermes(
    command: str, context: str, trigger: dict, timeout: int,
    resume_session: str | None = None,
) -> None:
    """Route la commande vocale vers H-Omar via webhook.

    H-Omar répond directement sur Telegram avec son contexte complet.
    La session est gérée par H-Omar lui-même — pas de session_id retourné.
    """
    template = RESUME_TEMPLATE if resume_session else PROMPT_TEMPLATE
    kanban_snapshot = await get_kanban_snapshot()
    prompt = template.format(
        keyword_label=trigger.get("label", "Allo Omar"),
        command=command or "(rien après le mot-clé — réagis au contexte ci-dessous)",
        context=context or "(pas de contexte disponible)",
        kanban_snapshot=kanban_snapshot,
    )
    await post_to_webhook(prompt)


async def run_hermes_raw(
    prompt: str, trigger: dict, timeout: int, resume_session: str | None = None,
    allow_actions: bool = False,
) -> None:
    """Variante bas niveau pour le mode conversation : prompt déjà construit.

    allow_actions est conservé pour compatibilité mais H-Omar a ses propres outils —
    le verrou n'est plus nécessaire, H-Omar décide lui-même ce qu'il exécute.
    """
    await post_to_webhook(prompt)


async def send_telegram(chat_id: int, text: str, inline_keyboard: list | None = None) -> None:
    """Envoie un message via H-Omar (hermes send) — notifications système, erreurs, ACK."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["(réponse vide)"]
    for chunk in chunks:
        try:
            proc = await asyncio.create_subprocess_exec(
                "hermes", "send", "--to", f"telegram:{chat_id}", "-q", chunk,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=15)
        except Exception as exc:
            log.error("hermes send échec: %s", exc)
