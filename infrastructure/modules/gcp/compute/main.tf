resource "google_compute_address" "this" {
  name   = var.name
  region = var.region
  labels = var.labels
}
resource "google_compute_instance" "this" {
  name                      = var.name
  zone                      = var.zone
  machine_type              = var.machine_type
  allow_stopping_for_update = true
  can_ip_forward            = var.can_ip_forward
  boot_disk {
    initialize_params {
      image  = var.image
      size   = var.boot_disk_gib
      type   = "pd-balanced"
      labels = var.labels
    }
  }
  network_interface {
    subnetwork = var.subnetwork_id
    access_config { nat_ip = google_compute_address.this.address }
  }
  service_account {
    email  = var.service_account_email
    scopes = ["cloud-platform"]
  }
  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
    startup-script         = var.startup_script
  }
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  labels = var.labels
}
