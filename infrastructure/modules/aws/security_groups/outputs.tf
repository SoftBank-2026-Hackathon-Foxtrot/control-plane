output "app_id" {
  value = aws_security_group.app.id
}

output "db_id" {
  value = aws_security_group.db.id
}

output "cache_id" {
  value = aws_security_group.cache.id
}
