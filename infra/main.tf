terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # After first apply, migrate state to S3 with:
  #   uncomment the backend block, run: terraform init -migrate-state
  # backend "s3" {
  #   bucket         = "dhan-trading-tfstate-155304839154"
  #   key            = "prod/terraform.tfstate"
  #   region         = "ap-south-1"
  #   dynamodb_table = "dhan-trading-tflock"
  #   encrypt        = true
  #   profile        = "dhan-terraform"
  # }
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
