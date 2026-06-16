terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # OPS-02: Remote state — S3 + DynamoDB locking. The state bucket name embeds the
  # AWS account id, which must NOT be committed (public repo). It is supplied at
  # init time via a gitignored backend.hcl (PARTIAL backend config):
  #
  #   cp backend.hcl.example backend.hcl   # then put the real bucket name in it
  #   terraform init -backend-config=backend.hcl            # (first time)
  #   terraform init -migrate-state -backend-config=backend.hcl  # (migrating local->S3)
  #
  # One-time bucket + lock-table creation is documented in
  # docs/Operations-Runbook.md ("Terraform remote state").
  backend "s3" {
    # bucket is provided via -backend-config=backend.hcl (account-id, not committed)
    key            = "dhan-trading/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "dhan-trading-tflock"
    encrypt        = true
    profile        = "dhan-terraform"
  }
}

provider "aws" {
  region  = var.aws_region
  profile = "dhan-terraform"

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.env
      ManagedBy   = "terraform"
    }
  }
}

# Latest Ubuntu 22.04 LTS ARM64 (Graviton) AMI — Canonical official
data "aws_ami" "ubuntu_arm64" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

locals {
  name_prefix = "${var.project}-${var.env}"
  ami_id      = data.aws_ami.ubuntu_arm64.id
}
