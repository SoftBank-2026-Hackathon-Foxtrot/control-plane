# One gateway instance: NAT for the private subnets, Tailscale subnet router (advertises the
# management VPC, accepts the app environments' routes), and the platform's cloudflared.
# No inbound port is open; administration is through SSM.

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "gateway" {
  name_prefix = "${var.name}-gateway-"
  description = "Platform gateway: forwards traffic from the VPC; no inbound from the internet"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-gateway" }
}
resource "aws_vpc_security_group_ingress_rule" "gateway_from_vpc" {
  security_group_id = aws_security_group.gateway.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "-1"
}
resource "aws_vpc_security_group_egress_rule" "gateway_all" {
  security_group_id = aws_security_group.gateway.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_iam_role" "gateway" {
  name = "${var.name}-gateway"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "gateway_ssm" {
  role       = aws_iam_role.gateway.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "gateway_secrets" {
  name = "gateway-secrets"
  role = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.tailscale_mgmt.arn, aws_secretsmanager_secret.tunnel_platform.arn]
    }]
  })
}
resource "aws_iam_instance_profile" "gateway" {
  name = "${var.name}-gateway"
  role = aws_iam_role.gateway.name
}

resource "aws_instance" "gateway" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.gateway_instance_type
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.gateway.id]
  iam_instance_profile        = aws_iam_instance_profile.gateway.name
  associate_public_ip_address = true
  source_dest_check           = false # it forwards packets that are not addressed to it
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/gateway.sh.tftpl", {
    region        = var.region
    vpc_cidr      = var.vpc_cidr
    tailscale_key = aws_secretsmanager_secret.tailscale_mgmt.arn
    tunnel_token  = aws_secretsmanager_secret.tunnel_platform.arn
    hostname      = "${var.name}-gw"
  })
  metadata_options {
    http_tokens = "required"
  }
  root_block_device {
    volume_type = "gp3"
    volume_size = 16
    encrypted   = true
  }
  tags = { Name = "${var.name}-gateway" }
  lifecycle {
    ignore_changes = [ami]
  }
}
