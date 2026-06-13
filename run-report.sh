#!/bin/bash
# Compte rendu quotidien omi-bridge → Telegram. Lancé par cron (22h).
cd /home/omar/32-Infra/integrations/omi-bridge
.venv/bin/python -m app.report >> /home/omar/32-Infra/integrations/omi-bridge/report.log 2>&1
