#!/usr/bin/env bash
# secrets-sync.sh — back up / restore the gitignored Terraform secret files to
# the private tfstate S3 bucket (AES256-encrypted, versioned, public-access
# blocked). These files are gitignored and must NEVER be committed; S3 is their
# durable backup so the single local working copy is not a loss risk.
#
#   ./secrets-sync.sh push    # local → s3://<bucket>/secrets/   (after editing tfvars)
#   ./secrets-sync.sh pull    # s3://<bucket>/secrets/ → local   (on a fresh clone)
#
# The bucket name (it embeds the account id) is read from the gitignored
# backend.hcl — nothing sensitive is hardcoded here, so this script is safe to
# commit to the public repo. apply.sh calls `push` after a successful apply.
set -euo pipefail
cd "$(dirname "$0")"
: "${AWS_PROFILE:=dhan-terraform}"; export AWS_PROFILE

BUCKET=$(grep -E '^[[:space:]]*bucket' backend.hcl 2>/dev/null | sed -E 's/.*"([^"]+)".*/\1/')
[ -n "${BUCKET:-}" ] || { echo "ERROR: could not read bucket from backend.hcl" >&2; exit 1; }
PREFIX="secrets"
FILES=(terraform.tfvars backend.hcl aws_outputs.local)

case "${1:-}" in
  push)
    for f in "${FILES[@]}"; do
      if [ -f "$f" ]; then
        aws s3 cp "$f" "s3://$BUCKET/$PREFIX/$f" --sse AES256 >/dev/null && echo "pushed  $f"
      fi
    done
    echo "secrets backed up to s3://<tfstate-bucket>/$PREFIX/ (AES256, versioned)" ;;
  pull)
    for f in "${FILES[@]}"; do
      aws s3 cp "s3://$BUCKET/$PREFIX/$f" "$f" >/dev/null 2>&1 && echo "pulled  $f" \
        || echo "skip    $f (not in S3)"
    done ;;
  *)
    echo "usage: $0 push|pull" >&2; exit 1 ;;
esac
