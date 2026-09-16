terraform {
  required_version = ">= 1.5.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      # Pinned to a minor-version range on purpose: an unbounded ">="
      # constraint let a plain `terraform init` silently pull in whatever
      # major version was newest (5.x -> 9.x over time) on a machine/CI
      # run that hadn't applied in a while. A provider schema change
      # between major versions can make Terraform see a spurious diff on
      # an otherwise-unchanged oci_core_instance and force-replace it --
      # which is exactly what destroyed a healthy, running instance right
      # before Oracle's own (unrelated) "out of host capacity" error
      # blocked the replacement from ever being created.
      version = "~> 9.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5.0"
    }
  }
}
