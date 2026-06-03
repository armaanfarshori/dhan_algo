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

  # SSH for operator maintenance (restrict to your IP in tfvars if needed)
  ingress {
    description = "SSH operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]  # tighten to your IP if desired
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

  # SSH
  ingress {
    description = "SSH operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Dashboard port — bind to localhost on the box; reach via SSH tunnel or Tailscale
  ingress {
    description = "Dashboard VPC-internal only, reach via SSH tunnel"
    from_port   = 8765
    to_port     = 8765
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]  # VPC-internal only
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
