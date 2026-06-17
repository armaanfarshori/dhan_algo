# ── DB security group — no public access, only from agent + SSH bastion ───────
resource "aws_security_group" "db" {
  name        = "${local.name_prefix}-sg-db"
  description = "TimescaleDB: inbound only from agent and operator SSH"
  vpc_id      = aws_vpc.main.id

  # PostgreSQL from agent
  ingress {
    description     = "TimescaleDB from agent"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.agent.id]
  }

  # SSH only from inside the VPC — the DB is in a private subnet with no public
  # IP and is reached via the agent jump host (connect.sh db -J agent). This is
  # the deliberate hardening applied 2026-06-17 (was var.ssh_allowed_cidr =
  # 0.0.0.0/0); codified here so `terraform apply` no longer reverts it.
  ingress {
    description = "SSH from within VPC (via agent jump host)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  # All outbound (apt, Docker pulls, S3 backups)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-sg-db" }
}

# ── Agent security group ──────────────────────────────────────────────────────
resource "aws_security_group" "agent" {
  name        = "${local.name_prefix}-sg-agent"
  description = "Trading agent: SSH + dashboard (VPN-only) + Dhan API egress"
  vpc_id      = aws_vpc.main.id

  # SSH — set ssh_allowed_cidr in tfvars to restrict to your operator IP
  ingress {
    description = "SSH operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_allowed_cidr]
  }

  # Dashboard port — bind to localhost on the box; reach via SSH tunnel or Tailscale
  ingress {
    description = "Dashboard VPC-internal only, reach via SSH tunnel"
    from_port   = 8765
    to_port     = 8765
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"] # VPC-internal only
  }

  # All outbound — Dhan API (api.dhan.co), package updates, S3
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-sg-agent" }
}
