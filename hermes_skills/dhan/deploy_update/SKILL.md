---
name: deploy_update
description: Safe rolling deploy — git pull, pip install, alembic upgrade, systemctl restart. User-triggered only, never cron.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Deploy,Update,Manual,Safe]
    category: dhan-trading
---

# Deploy Update Skill

Safe rolling deploy. Always user-triggered — never automated.

## How to Use
Tell Hermes: 'deploy latest code' or 'update the agent'
Hermes will confirm before executing.
