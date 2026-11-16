terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # Estado remoto: copie backend.tf.example a backend.tf y ajuste el bucket.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "usagehub"
      Environment = var.environment
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}
