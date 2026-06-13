"""Dispatch des commandes vocales vers Hermes + réponses Telegram."""
import asyncio
import logging
import re

import httpx

from .config import CONFIG, TELEGRAM_BOT_TOKEN

log = logging.getLogger("omi-bridge.dispatch")

# Anti-burst : limite le nombre de `hermes chat` simultanés (règle OA : max 5)
_hermes_semaphore = asyncio.Semaphore(CONFIG["limits"]["max_concurrent_hermes"])

PROMPT_TEMPLATE = """Tu reçois une demande vocale d'Alex, captée par son wearable OMI (commande "{keyword_label}").
Ta réponse sera envoyée telle quelle sur Telegram à Alex : réponds en français, de façon concise et directe (pas de markdown lourd, pas de préambule).

⚠️ MODE CONSULTATIF — RÈGLE ABSOLUE :
Tu peux LIRE et te RENSEIGNER (consulter l'état, chercher, répondre à une question, faire un calcul).
Tu NE DOIS JAMAIS exécuter d'action qui modifie quoi que ce soit : pas de création/modification/suppression
de fichier, carte kanban, post, déploiement, build, envoi de message à un tiers, commande système.
Une commande vocale ambiante peut être mal transcrite ou ne pas venir d'Alex : dans le doute, tu ne fais RIEN.
Si la demande implique une action de modification, NE L'EXÉCUTE PAS : décris en une phrase le plan que tu
proposes, et précise qu'Alex doit confirmer par « {keyword_label} c'est parti » (mode conversation) pour lancer.
La transcription vocale peut contenir des erreurs : interprète avec bon sens, mais reste en lecture seule.

DEMANDE D'ALEX :
{command}

CONTEXTE (ce qui se disait juste avant, même conversation) :
{context}"""

# Sur reprise de session, les instructions sont déjà dans la conversation hermes
RESUME_TEMPLATE = """Nouvelle demande vocale d'Alex via OMI (suite de notre conversation) :
{command}

Contexte audio récent :
{context}"""

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


async def run_hermes(
    command: str, context: str, trigger: dict, timeout: int,
    resume_session: str | None = None,
) -> tuple[str, str | None]:
    """Lance `hermes chat -Q` et retourne (réponse, session_id hermes).

    Si resume_session est fourni, la conversation hermes précédente est
    reprise (--resume) : c'est ce qui donne UNE conversation continue à
    travers plusieurs commandes vocales.
    """
    template = RESUME_TEMPLATE if resume_session else PROMPT_TEMPLATE
    prompt = template.format(
        keyword_label=trigger.get("label", "Allo Omar"),
        command=command or "(rien après le mot-clé — réagis au contexte ci-dessous)",
        context=context or "(pas de contexte disponible)",
    )
    # Commande ponctuelle = toujours consultatif (zéro outil)
    return await run_hermes_raw(prompt, trigger, timeout, resume_session, allow_actions=False)


async def run_hermes_raw(
    prompt: str, trigger: dict, timeout: int, resume_session: str | None = None,
    allow_actions: bool = False,
) -> tuple[str, str | None]:
    """Variante bas niveau : prompt déjà construit (mode conversation).

    allow_actions=False (défaut) → VERROU DUR : hermes tourne sans aucun outil
    (`-t ''`), il peut répondre mais ne peut PHYSIQUEMENT rien modifier.
    allow_actions=True → outils complets, réservé à l'exécution explicite
    (« c'est parti ») validée par la voix d'Alex.
    """
    # hermes_extra_args (config) permet de cibler un autre agent/modèle,
    # ex. ["-m", "anthropic/claude-sonnet-4"] — par défaut : profil hermes courant (h-omar)
    args = ["hermes", "chat", "-Q", "-q", prompt] + list(trigger.get("hermes_extra_args", []))
    if not allow_actions:
        # Sentinelle : un nom de toolset inexistant => hermes ne charge AUCUN
        # outil (vérifié : `-t ''` est ignoré et retombe sur la config par
        # défaut, alors que `-t none` ne charge rien). Verrou lecture seule.
        args += ["-t", "none"]
    if resume_session:
        args += ["--resume", resume_session]

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

    # hermes écrit "session_id: <id>" sur stderr en mode -Q
    session_id = None
    match = re.search(r"session_id:\s*(\S+)", stderr.decode())
    if match:
        session_id = match.group(1)

    # Filtre le warning du verrou lecture seule (`-t none`) qui sort sur stdout
    out = "\n".join(
        ln for ln in stdout.decode().splitlines()
        if not ln.strip().startswith("Warning: Unknown toolsets")
    ).strip()
    return out, session_id


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
