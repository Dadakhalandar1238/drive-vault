# ---------------------------------------------------------------------
# OCI auth -- reuses your OCI CLI config (~/.oci/config) rather than
# asking you to paste a private key path/fingerprint into a tfvars file.
# Generate it once with: oci setup config
# ---------------------------------------------------------------------
variable "oci_config_profile" {
  description = "Profile name in ~/.oci/config (default profile is [DEFAULT])."
  type        = string
  default     = "DEFAULT"
}

variable "region" {
  description = "OCI region to deploy into, e.g. us-ashburn-1. Must be a region where Ampere A1 Always-Free capacity is available to your tenancy."
  type        = string
}

variable "compartment_ocid" {
  description = "OCID of the compartment to create resources in. Your tenancy's root compartment OCID works fine for a personal deployment -- find it under Console -> Profile -> Tenancy."
  type        = string
}

# ---------------------------------------------------------------------
# Compute shape -- Ampere A1.Flex is the generous "Always Free" shape:
# up to 4 OCPUs / 24 GB RAM total across all A1 instances in a tenancy.
# Defaults below use all of it for this one instance; lower them if you
# plan to run other Always-Free A1 instances too.
# ---------------------------------------------------------------------
variable "instance_ocpus" {
  type    = number
  default = 4
}

variable "instance_memory_in_gbs" {
  type    = number
  default = 24
}

variable "availability_domain_index" {
  description = "Index into the list of availability domains in the region (0 = first). Change this if the first AD doesn't have A1 capacity available."
  type        = number
  default     = 0
}

variable "ssh_public_key_path" {
  description = "Path to a local SSH public key file, used for SSH access to the instance."
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

# ---------------------------------------------------------------------
# App source + config. GOOGLE_CLIENT_ID/SECRET are NOT set here on
# purpose -- this app is bring-your-own-OAuth, so every end user enters
# their own credentials through the app's onboarding screen. Nothing
# Google-related needs to be configured by the deployer.
# ---------------------------------------------------------------------
variable "repo_url" {
  description = "Git URL to deploy from."
  type        = string
  default     = "https://github.com/Dadakhalandar1238/drive-vault.git"
}

variable "git_ref" {
  description = "Branch, tag, or commit to deploy."
  type        = string
  default     = "main"
}

variable "domain" {
  description = "Your own domain name pointed at this instance's public IP (an A record), used for the HTTPS certificate. Leave blank to auto-use a free nip.io hostname derived from the instance's public IP instead -- zero DNS setup required."
  type        = string
  default     = ""
}

variable "google_picker_api_key" {
  description = "Optional -- only enables the 'Import from Drive' (Google Picker) button. Safe to share across all users of this deployment (unlike an OAuth Client ID/Secret, it doesn't grant Drive access by itself)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "max_drives_per_user" {
  type    = number
  default = 10
}

variable "max_workers" {
  type    = number
  default = 4
}
