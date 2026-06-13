"""Chargement de la configuration omi-bridge : config.yaml + .env."""
import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("OMI_BRIDGE_CONFIG", BASE_DIR / "config.yaml"))
ENV_PATH = Path(os.environ.get("OMI_BRIDGE_ENV", BASE_DIR / ".env"))
DB_PATH = Path(os.environ.get("OMI_BRIDGE_DB", BASE_DIR / "omi-bridge.db"))


def _load_env(path: Path) -> None:
    """Parse minimaliste d'un .env (KEY=VALUE, # commentaires)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(ENV_PATH)

WEBHOOK_SECRET = os.environ.get("OMI_BRIDGE_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

if not WEBHOOK_SECRET:
    raise RuntimeError("OMI_BRIDGE_SECRET manquant (.env)")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("context", {})
    cfg["context"].setdefault("pre_trigger_segments", 25)
    cfg["context"].setdefault("command_silence_seconds", 6)
    cfg["context"].setdefault("session_resume_hours", 12)
    cfg.setdefault("limits", {})
    cfg["limits"].setdefault("max_concurrent_hermes", 2)
    cfg["limits"].setdefault("hermes_timeout_seconds", 240)
    return cfg


CONFIG = load_config()


def resolve_user(uid: str) -> dict | None:
    """Retourne la config user pour un uid OMI ('*' = wildcard)."""
    wildcard = None
    for user in CONFIG.get("users", []):
        if user.get("uid") == uid:
            return user
        if user.get("uid") == "*":
            wildcard = user
    return wildcard
