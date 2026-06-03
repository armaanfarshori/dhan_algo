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

variable "groq_api_key" {
  description = "Groq API key for Hermes LLM orchestrator (console.groq.com)"
  type        = string
  sensitive   = true
}
