terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "image" {
  type        = string
  description = "Container image for test-cross-repo-app"
}

variable "environment" {
  type        = string
  description = "Deployment environment (dev or prod)"
}
