output "db_private_ip" {
  description = "TimescaleDB server private IP (use this in agent .env DB_HOST)"
  value       = aws_instance.db.private_ip
}

output "agent_elastic_ip" {
  description = "Agent Elastic IP — WHITELIST THIS in Dhan DevPortal before enabling live orders"
  value       = aws_eip.agent.public_ip
}

output "agent_instance_id" {
  value = aws_instance.agent.id
}

output "db_instance_id" {
  value = aws_instance.db.id
}

output "s3_bucket" {
  description = "S3 bucket for backups, archives, Kronos checkpoints"
  value       = aws_s3_bucket.data.bucket
}

output "ssh_db" {
  description = "SSH command for DB server (via agent as jump host)"
  value       = "ssh -J ubuntu@${aws_eip.agent.public_ip} ubuntu@${aws_instance.db.private_ip}"
}

output "ssh_agent" {
  description = "SSH command for agent server"
  value       = "ssh ubuntu@${aws_eip.agent.public_ip}"
}

output "monthly_cost_estimate" {
  description = "Approximate monthly AWS cost (ap-south-1)"
  value = {
    db_instance    = "${var.db_instance_type} ~$25/mo"
    agent_instance = "${var.agent_instance_type} ~$6/mo"
    ebs_db_data    = "${var.db_disk_gb}GB gp3 ~$18/mo"
    ebs_root_x2    = "2x 30GB gp3 ~$5/mo"
    elastic_ip     = "attached = $0/mo"
    s3             = "~$2/mo"
    total          = "~$56/mo"
  }
}
