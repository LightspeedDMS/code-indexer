variable "environment" {
  type        = string
  description = "Deployment environment name (dev, stage, prod)"
}

variable "region" {
  type        = string
  default     = "us-east-2"
  description = "AWS region"
}
