output "instance_id" {
  value = aws_instance.this.id
}

output "public_ip" {
  value = aws_eip.this.public_ip
}

output "private_ip" {
  value = aws_instance.this.private_ip
}

output "source_dest_check" {
  value = aws_instance.this.source_dest_check
}
