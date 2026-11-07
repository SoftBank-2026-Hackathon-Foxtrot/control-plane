output "instance_name" {
  value = google_sql_database_instance.this.name
}

output "private_ip" {
  value = google_sql_database_instance.this.private_ip_address
}

output "connection_name" {
  value = google_sql_database_instance.this.connection_name
}

output "admin_user" {
  value = google_sql_user.admin.name
}
output "public_ip" {
  value = google_sql_database_instance.this.public_ip_address
}
output "outgoing_ip" {
  description = "이 인스턴스가 다른 DB로 복제 접속할 때 쓰는 IP. 원본 DB의 허용 목록에 넣습니다."
  value       = try([for a in google_sql_database_instance.this.ip_address : a.ip_address if a.type == "OUTGOING"][0], null)
}

output "app_db_user" { value = google_sql_user.app.name }
