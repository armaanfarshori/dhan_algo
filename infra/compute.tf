# ── SSH key pair ──────────────────────────────────────────────────────────────
resource "aws_key_pair" "operator" {
  key_name   = "${local.name_prefix}-key"
  public_key = var.ssh_public_key
}

# ── DB server — TimescaleDB (private, no Elastic IP) ─────────────────────────
resource "aws_instance" "db" {
  ami                    = local.ami_id
  instance_type          = var.db_instance_type
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.db.id]
  key_name               = aws_key_pair.operator.key_name
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  # No public IP — agent reaches it via private IP inside VPC
  associate_public_ip_address = false

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    delete_on_termination = true
    encrypted             = true
  }

  tags = { Name = "${local.name_prefix}-db", Role = "timescaledb" }

  user_data = base64encode(templatefile("${path.module}/scripts/setup_db.sh", {
    project    = var.project
    aws_region = var.aws_region
    s3_bucket  = aws_s3_bucket.data.bucket
    ssm_prefix = "/${var.project}"
  }))

  lifecycle {
    # associate_public_ip_address drifts to true once an EIP is attached;
    # ignoring it prevents a destructive instance replacement on re-apply.
    ignore_changes = [ami, user_data, associate_public_ip_address]
  }
}

# ── Separate EBS data volume for TimescaleDB (survives instance replacement) ──
resource "aws_ebs_volume" "db_data" {
  availability_zone = "${var.aws_region}a"
  size              = var.db_disk_gb
  type              = "gp3"
  iops              = 3000
  throughput        = 125
  encrypted         = true

  tags = {
    Name   = "${local.name_prefix}-db-data"
    Backup = "dlm" # targeted by aws_dlm_lifecycle_policy in storage.tf (OPS-09)
  }
}

resource "aws_volume_attachment" "db_data" {
  device_name  = "/dev/sdf"
  volume_id    = aws_ebs_volume.db_data.id
  instance_id  = aws_instance.db.id
  force_detach = false
}

# ── Agent server — trading engine (public subnet, Elastic IP) ─────────────────
resource "aws_instance" "agent" {
  ami                    = local.ami_id
  instance_type          = var.agent_instance_type
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.agent.id]
  key_name               = aws_key_pair.operator.key_name
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  associate_public_ip_address = false # Elastic IP handles this

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    delete_on_termination = true
    encrypted             = true
  }

  tags = { Name = "${local.name_prefix}-agent", Role = "trading-agent" }

  user_data = base64encode(templatefile("${path.module}/scripts/setup_agent.sh", {
    project    = var.project
    aws_region = var.aws_region
    s3_bucket  = aws_s3_bucket.data.bucket
    ssm_prefix = "/${var.project}"
    db_host    = aws_instance.db.private_ip
  }))

  lifecycle {
    # associate_public_ip_address drifts to true once an EIP is attached;
    # ignoring it prevents a destructive instance replacement on re-apply.
    ignore_changes = [ami, user_data, associate_public_ip_address]
  }
}

# ── Elastic IP — static, whitelisted in Dhan DevPortal ───────────────────────
resource "aws_eip" "agent" {
  domain   = "vpc"
  instance = aws_instance.agent.id

  tags = { Name = "${local.name_prefix}-eip-agent", Purpose = "dhan-api-whitelist" }
}
