#!/bin/bash
# Compte rendu quotidien omi-bridge → Telegram. Lancé par cron (21h30).
cd /home/omar/32-Infra/integrations/omi-bridge
echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] lancement rapport" >> report.log
.venv/bin/python -m app.report >> report.log 2>&1
echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] rapport envoyé" >> report.log
