---
name: db_backup
description: User-triggered on-demand pg_dump to S3. Use before risky operations. Nightly backup runs automatically via cron.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Database,Backup,S3,Manual]
    category: dhan-trading
---

# Db Backup Skill

Manual trigger for emergency backup. Nightly backup already runs at 02:00 IST via cron on the DB server.

## How to Use
Tell Hermes: 'backup the database' or 'take a DB snapshot'
