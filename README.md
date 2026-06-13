# omi-bridge

Pont entre le wearable [OMI](https://omi.me) et les agents Hermes OA.
Reçoit les webhooks de l'app OMI officielle (transcripts temps réel, memories,
day summaries), détecte des mots-clés vocaux (« Allo Omar ») et route la
demande + son contexte vers un agent Hermes, qui répond sur Telegram.

```
OMI device → app OMI (téléphone) → cloud OMI (STT)
   → webhooks HTTPS → omi.<domaine> (Caddy, public + token secret)
      → omi-bridge (FastAPI :9998)
         ├─ mot-clé détecté → hermes chat → réponse Telegram
         └─ memories / day summaries → SQLite (UI bilan quotidien)
```

## Pourquoi pas le backend OMI self-hosted ?

Le backend officiel exige Firebase + Deepgram + Pinecone + GCS et un rebuild
de l'app mobile. Ce bridge utilise l'app officielle telle quelle : on colle
simplement des URLs dans Settings → Developer Mode → Developer Settings.

## Installation (un VPS par foyer d'agents)

```bash
git clone https://github.com/omar-paris/omi-bridge && cd omi-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # adapter users/triggers
cp .env.example .env                 # OMI_BRIDGE_SECRET=$(openssl rand -hex 24) + token bot Telegram
mkdir -p ~/.config/systemd/user && cp omi-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now omi-bridge
```

Exposer ensuite le port 9998 en HTTPS public (exemple Caddy dans `caddy/`).

## Configuration côté app OMI (chaque utilisateur)

Settings → Developer Mode → Developer Settings :

| Champ | URL |
|---|---|
| Realtime audio transcript | `https://omi.<domaine>/<SECRET>/webhook/transcript` |
| Memory creation webhook | `https://omi.<domaine>/<SECRET>/webhook/memory` |
| Day summary webhook | `https://omi.<domaine>/<SECRET>/webhook/day-summary` |

Multi-utilisateur : OMI ajoute `?uid=<id>` à chaque requête ; mapper les uid
dans `config.yaml` (`"*"` = wildcard). Chaque user a son chat Telegram et ses
propres triggers (« Allo Omar » → agent business, « Allo Aurel » → agent
perso sur un autre VPS, etc.).

## Détails de fonctionnement

- **Détection** : normalisation (minuscules, sans accents/ponctuation) puis
  recherche des variantes configurées (« allo omar », « a l eau omar »…),
  y compris à cheval sur deux segments de transcription.
- **Commande vocale** : tout ce qui suit le mot-clé, jusqu'à
  `command_silence_seconds` (défaut 6 s) sans nouveau segment.
- **Contexte** : les `pre_trigger_segments` derniers segments de la même
  session sont fournis à l'agent.
- **Anti-burst** : `max_concurrent_hermes` limite les `hermes chat`
  simultanés (règle OA : max 5).
- **Stockage** : tout est conservé en SQLite (`segments`, `memories`,
  `day_summaries`, `commands`) — base de la future UI de bilan quotidien.

## Sécurité

Endpoint public par nécessité (le cloud OMI doit pouvoir POSTer). Mitigations :
token 48 hex dans le chemin (404 systématique sinon), aucune route sans
secret, pas de contenu servi, payloads bornés par FastAPI, service non-root
(systemd user), reverse-proxy TLS via Caddy.
