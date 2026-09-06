output "public_ip" {
  description = "The instance's public IP address."
  value       = data.oci_core_vnic.app.public_ip_address
}

output "app_url" {
  description = "Visit this once the instance finishes booting (a few minutes -- Docker build + first TLS cert issuance)."
  value       = var.domain != "" ? "https://${var.domain}" : "https://${replace(data.oci_core_vnic.app.public_ip_address, ".", "-")}.nip.io"
}

output "ssh_command" {
  description = "SSH in to check on things (cloud-init log: /var/log/cloud-init-output.log, app: docker compose -f /opt/drive-vault/docker-compose.yml logs)."
  value       = "ssh ubuntu@${data.oci_core_vnic.app.public_ip_address}"
}
