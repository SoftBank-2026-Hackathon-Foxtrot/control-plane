resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
  tags       = var.tags
}
resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg${split(".", var.engine_version)[0]}"
  family = "postgres${split(".", var.engine_version)[0]}"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  # Logical replication lets the DB migration agent copy this database to another cloud
  # (publication/subscription). It needs a reboot, so it is set from the first apply.
  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }
  tags = var.tags
}
resource "aws_db_instance" "this" {
  identifier                      = var.name
  engine                          = "postgres"
  engine_version                  = var.engine_version
  instance_class                  = var.instance_class
  db_name                         = var.database_name
  username                        = "platformadmin"
  manage_master_user_password     = true
  allocated_storage               = var.storage_gib
  max_allocated_storage           = var.max_storage_gib
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [var.security_group_id]
  parameter_group_name            = aws_db_parameter_group.this.name
  publicly_accessible             = var.publicly_accessible
  multi_az                        = var.multi_az
  backup_retention_period         = 7
  backup_window                   = "18:00-19:00"
  maintenance_window              = "sun:19:00-sun:20:00"
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = false
  final_snapshot_identifier       = var.final_snapshot_identifier
  copy_tags_to_snapshot           = true
  auto_minor_version_upgrade      = true
  apply_immediately               = false
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  tags                            = var.tags
}
