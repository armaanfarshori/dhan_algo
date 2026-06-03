---
name: kronos_finetune_trigger
description: When DB has >1 year data for 500+ securities, instructions to launch g4dn.xlarge spot GPU and fine-tune Kronos-base on NSE bars.
version: 0.1.0
author: DhanAIBot
platforms: [linux]
metadata:
  hermes:
    tags: [Kronos,Finetune,GPU,AI]
    category: dhan-trading
---

# Kronos Finetune Trigger Skill

Instructions to launch GPU fine-tuning when data volume is sufficient.

## When ready
- DB has >1 year of 1m data for >500 securities ✓ check with `/api/db/stats`
- NSE distribution gap confirmed via `signal_calibration` accuracy <55%

## Launch command (on agent EC2)
```bash
aws ec2 run-instances --instance-type g4dn.xlarge \
  --image-id ami-... --spot-market-options MaxPrice=0.50 \
  --user-data file://infra/scripts/setup_finetune.sh
```
Checkpoint exported to S3, loaded via KRONOS_CHECKPOINT env var.
