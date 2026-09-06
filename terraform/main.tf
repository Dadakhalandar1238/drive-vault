provider "oci" {
  config_file_profile = var.oci_config_profile
  region              = var.region
}

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_ocid
}

# Latest available Ubuntu image for the Ampere A1 shape in this region --
# not pinned to a specific Ubuntu version so this keeps working as Oracle
# rotates images.
data "oci_core_images" "ubuntu_arm" {
  compartment_id   = var.compartment_ocid
  operating_system = "Canonical Ubuntu"
  shape            = "VM.Standard.A1.Flex"
  sort_by          = "TIMECREATED"
  sort_order       = "DESC"
}

resource "random_id" "secret_key" {
  byte_length = 32
}

# ---------------------------------------------------------------------
# Networking -- a minimal public VCN: one subnet, an internet gateway,
# and a security list open on 22 (SSH), 80 (HTTP/ACME), 443 (HTTPS).
# ---------------------------------------------------------------------
resource "oci_core_vcn" "vcn" {
  compartment_id = var.compartment_ocid
  display_name   = "drive-vault-vcn"
  cidr_blocks    = ["10.0.0.0/16"]
  dns_label      = "drivevault"
}

resource "oci_core_internet_gateway" "igw" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "drive-vault-igw"
  enabled        = true
}

resource "oci_core_route_table" "rt" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "drive-vault-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_internet_gateway.igw.id
  }
}

resource "oci_core_security_list" "sl" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "drive-vault-sl"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "6" # TCP
    tcp_options {
      min = 22
      max = 22
    }
  }
  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "6"
    tcp_options {
      min = 80
      max = 80
    }
  }
  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "6"
    tcp_options {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_subnet" "subnet" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.vcn.id
  cidr_block                 = "10.0.0.0/24"
  display_name               = "drive-vault-subnet"
  dns_label                  = "app"
  route_table_id             = oci_core_route_table.rt.id
  security_list_ids          = [oci_core_security_list.sl.id]
  prohibit_public_ip_on_vnic = false
}

# ---------------------------------------------------------------------
# Compute instance
# ---------------------------------------------------------------------
resource "oci_core_instance" "app" {
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[var.availability_domain_index].name
  display_name        = "drive-vault"
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = var.instance_ocpus
    memory_in_gbs = var.instance_memory_in_gbs
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.subnet.id
    assign_public_ip = true
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.ubuntu_arm.images[0].id
  }

  metadata = {
    ssh_authorized_keys = file(pathexpand(var.ssh_public_key_path))
    user_data = base64encode(templatefile("${path.module}/cloud-init.sh.tftpl", {
      repo_url              = var.repo_url
      git_ref               = var.git_ref
      domain                = var.domain
      secret_key            = random_id.secret_key.b64_url
      google_picker_api_key = var.google_picker_api_key
      max_drives_per_user   = var.max_drives_per_user
      max_workers           = var.max_workers
    }))
  }
}

data "oci_core_vnic_attachments" "app" {
  compartment_id = var.compartment_ocid
  instance_id    = oci_core_instance.app.id
}

data "oci_core_vnic" "app" {
  vnic_id = data.oci_core_vnic_attachments.app.vnic_attachments[0].vnic_id
}
