# ── S3 bucket — backups, Parquet archives, Kronos checkpoints ────────────────
resource "aws_s3_bucket" "data" {
  bucket        = "${var.project}-data-${data.aws_caller_identity.current.account_id}"
  force_destroy = false

  tags = { Name = "${local.name_prefix}-data" }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lifecycle: move raw tick archives to Glacier after 90 days
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "archive-ticks"
    status = "Enabled"

    filter { prefix = "ticks/" }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }

  rule {
    id     = "expire-old-backups"
    status = "Enabled"

    filter { prefix = "db-backups/" }

    expiration { days = 365 }
  }
}

# ── SSM Parameter Store — secrets (encrypted, never in git) ──────────────────
resource "aws_ssm_parameter" "db_password" {
  name        = "/${var.project}/db_password"
  type        = "SecureString"
  value       = var.db_password
  description = "TimescaleDB postgres password"
  overwrite   = true
}

resource "aws_ssm_parameter" "dhan_client_id" {
  name        = "/${var.project}/dhan_client_id"
  type        = "SecureString"
  value       = var.dhan_client_id
  overwrite   = true
}

resource "aws_ssm_parameter" "dhan_access_token" {
  name        = "/${var.project}/dhan_access_token"
  type        = "SecureString"
  value       = var.dhan_access_token
  overwrite   = true
}

resource "aws_ssm_parameter" "dhan_totp_secret" {
  name        = "/${var.project}/dhan_totp_secret"
  type        = "SecureString"
  value       = var.dhan_totp_secret
  overwrite   = true
}

resource "aws_ssm_parameter" "dhan_pin" {
  name        = "/${var.project}/dhan_pin"
  type        = "SecureString"
  value       = var.dhan_pin
  overwrite   = true
}

resource "aws_ssm_parameter" "groq_api_key" {
  name        = "/${var.project}/groq_api_key"
  type        = "SecureString"
  value       = var.groq_api_key
  description = "Groq API key for Hermes LLM orchestrator"
  overwrite   = true
}
