#
# Módulo reutilizable: SPA estática en S3 privado servida por CloudFront con OAC,
# TLS y cabeceras de seguridad (CSP, HSTS, X-Frame-Options...).
#

resource "aws_s3_bucket" "this" {
  # checkov:skip=CKV_AWS_18: el acceso se registra en CloudFront; el bucket solo lo lee CloudFront por OAC
  # checkov:skip=CKV_AWS_144: contenido estático reproducible desde Git; replicar entre regiones no aporta recuperación
  # checkov:skip=CKV_AWS_145: archivos públicos de la SPA, sin datos; SSE-S3 basta y CloudFront no necesita permisos de KMS
  # checkov:skip=CKV2_AWS_62: nada reacciona a cambios del bucket; lo actualiza solo el despliegue
  bucket_prefix = "${var.name}-"
  force_destroy = var.force_destroy
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versiones para revertir un despliegue malo; las anteriores se borran a los 30 días.
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    id     = "limpiar-versiones"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "this" {
  name                              = "${var.name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_response_headers_policy" "security" {
  name = "${var.name}-security-headers"
  security_headers_config {
    content_security_policy {
      override                = true
      content_security_policy = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' ${join(" ", var.connect_src)}; frame-ancestors 'none'; base-uri 'self'; form-action 'self' ${join(" ", var.connect_src)}"
    }
    strict_transport_security {
      override                   = true
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      override     = true
      frame_option = "DENY"
    }
    referrer_policy {
      override        = true
      referrer_policy = "strict-origin-when-cross-origin"
    }
  }
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

resource "aws_cloudfront_distribution" "this" {
  # checkov:skip=CKV_AWS_68: WAF tiene costo fijo; el sitio es estático y la API aplica throttling y JWT (ver docs/security.md)
  # checkov:skip=CKV2_AWS_47: depende de WAF (CKV_AWS_68)
  # checkov:skip=CKV2_AWS_42: sin dominio propio en el prototipo; usa el certificado de *.cloudfront.net
  # checkov:skip=CKV_AWS_174: con el certificado por defecto CloudFront no permite fijar TLS 1.2; se resuelve con dominio propio y ACM
  # checkov:skip=CKV_AWS_86: logs de acceso de CloudFront desactivados por costo; la API registra cada llamada
  # checkov:skip=CKV_AWS_310: un solo origen S3 regional, con 99,99 % de disponibilidad; failover recomendado en producción
  # checkov:skip=CKV_AWS_374: el producto no restringe países
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  comment             = "${var.name} dashboard"

  origin {
    domain_name              = aws_s3_bucket.this.bucket_regional_domain_name
    origin_id                = "s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.this.id
  }

  default_cache_behavior {
    target_origin_id           = "s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1.2_2021"
  }
  tags = var.tags
}

data "aws_iam_policy_document" "bucket" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.bucket.json
}

locals {
  content_types = { html = "text/html", css = "text/css", js = "application/javascript", json = "application/json", svg = "image/svg+xml" }
}

resource "aws_s3_object" "assets" {
  for_each      = fileset(var.source_dir, "**")
  bucket        = aws_s3_bucket.this.id
  key           = each.value
  source        = "${var.source_dir}/${each.value}"
  etag          = filemd5("${var.source_dir}/${each.value}")
  content_type  = lookup(local.content_types, reverse(split(".", each.value))[0], "application/octet-stream")
  cache_control = "no-cache"
}

resource "aws_s3_object" "config" {
  bucket        = aws_s3_bucket.this.id
  key           = "config.json"
  content       = jsonencode(var.runtime_config)
  content_type  = "application/json"
  cache_control = "no-cache"
}
