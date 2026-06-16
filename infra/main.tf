terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # OPS-02: Remote state — S3 + DynamoDB locking.
  # OPERATOR SETUP (one-time, before running terraform init -migrate-state):
  #   1. aws s3api create-bucket --bucket dhan-trading-tfstate-<ACCOUNT_ID> \
  #         --region ap-south-1 \
  #         --create-bucket-configuration LocationConstraint=ap-south-1
  #   2. aws s3api put-bucket-versioning --bucket dhan-trading-tfstate-<ACCOUNT_ID> \
  #         --versioning-configuration Status=Enabled
  #   3. aws dynamodb create-table --table-name dhan-trading-tflock \
  #         --attribute-definitions AttributeName=LockID,AttributeType=S \
  #         --key-schema AttributeName=LockID,KeyType=HASH \
  #         --billing-mode PAY_PER_REQUEST --region ap-south-1
  #   4. Replace <ACCOUNT_ID> below with your real AWS account ID, then run:
  #         terraform init -migrate-state
  # Backend blocks cannot use variables — fill the literal values here.
  backend "s3" {
    bucket         = "dhan-trading-tfstate-<ACCOUNT_ID>" # replace <ACCOUNT_ID>
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
