variable "aws_region" {
  default = "ap-south-1"
}

variable "project" {
  default = "dhan-trading"
}

variable "env" {
  default = "prod"
}

# Your SSH public key — paste output of: cat ~/.ssh/id_ed25519.pub
variable "ssh_public_key" {
  description = "SSH public key for EC2 access"
  type        = string
}

# ── Instance sizing ───────────────────────────────────────────────────────────
variable "db_instance_type" {
  default     = "t4g.medium"
  description = "TimescaleDB server (4GB RAM, handles 500M+ rows)"
}

variable "agent_instance_type" {
  default     = "t4g.micro"
  description = "Trading agent (t4g.small during free-trial period)"
}

# ── Storage ───────────────────────────────────────────────────────────────────
variable "db_disk_gb" {
  default     = 200
  description = "EBS gp3 volume for TimescaleDB data (GB)"
}

# ── DB credentials ────────────────────────────────────────────────────────────
# OPS-03: SSH ingress CIDR — MUST be overridden to your operator IP in terraform.tfvars.
# Leaving the default "0.0.0.0/0" exposes SSH to the entire internet; restrict to your
# static IP or VPN egress (e.g. "1.2.3.4/32") before running terraform apply.
variable "ssh_allowed_cidr" {
  description = "CIDR allowed to reach SSH port 22 on both security groups. Override this to your operator IP (e.g. '1.2.3.4/32'). Default is open — INSECURE, must be tightened in tfvars."
  type        = string
  default     = "0.0.0.0/0" # WARNING: open to world — override in terraform.tfvars
}

variable "db_password" {
  description = "TimescaleDB postgres password — stored in SSM, not in state"
  type        = string
  sensitive   = true
}

# ── Dhan secrets ──────────────────────────────────────────────────────────────
variable "dhan_client_id" {
  type      = string
  sensitive = true
}

variable "dhan_access_token" {
  type      = string
  sensitive = true
}

variable "dhan_totp_secret" {
  type      = string
  sensitive = true
}

variable "dhan_pin" {
  type      = string
  sensitive = true
}
