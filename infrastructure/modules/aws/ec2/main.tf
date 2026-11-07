resource "aws_instance" "this" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [var.security_group_id]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = true
  key_name                    = var.key_name
  source_dest_check           = var.source_dest_check
  user_data_base64            = var.user_data == null ? null : base64gzip(var.user_data)
  user_data_replace_on_change = true
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_disk_gib
    encrypted   = true
    tags        = merge(var.tags, { Name = "${var.name}-root" })
  }
  tags = merge(var.tags, { Name = var.name, exposure = "public" })
}
resource "aws_eip" "this" {
  domain   = "vpc"
  instance = aws_instance.this.id
  tags     = merge(var.tags, { Name = var.name })
}
