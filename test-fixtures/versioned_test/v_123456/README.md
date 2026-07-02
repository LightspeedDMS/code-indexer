# CIDX Multimodal Test Fixtures

This repository contains curated test fixtures for E2E testing of CIDX multimodal image vectorization.

## Purpose

Provides controlled, reproducible test data for verifying:
- Markdown image extraction and indexing
- HTML/HTMX image extraction and indexing
- Edge case handling (missing files, remote URLs, unsupported formats)
- Semantic search of image content

## Directory Structure

```
.
├── docs/
│   ├── database-guide.md          # MD with PNG (database schema table)
│   ├── api-reference.md           # MD with JPEG (API flow diagram)
│   ├── configuration.html         # HTML with WebP (config options)
│   ├── troubleshooting.htmx       # HTMX with GIF (error codes table)
│   └── edge-cases/
│       ├── missing-image.md       # References non-existent image
│       ├── remote-url.md          # References http/https URLs
│       ├── unsupported-format.md  # References BMP file
│       ├── text-looks-like-image.md # Text resembling paths (not valid syntax)
│       └── mixed-valid-invalid.md # Mix of valid and invalid references
└── images/
    ├── database-schema.png        # Table: user_id, username, email, created_at, password_hash
    ├── api-flow.jpg               # Diagram: Authentication, Token Validation, Protected Resource
    ├── config-options.webp        # Config: server.port, database.url, cache.ttl, etc.
    ├── error-codes.gif            # Table: 400, 401, 403, 404, 429, 500, 502, 503
    ├── unsupported.bmp            # For edge case testing (unsupported format)
    └── oversized-placeholder.png  # Placeholder documenting oversized image test
```

## Expected Search Results

### Query: "database schema"
- Should match: `docs/database-guide.md` (via image content)
- Image contains: Column names, data types, descriptions for users table

### Query: "authentication flow" or "JWT token"
- Should match: `docs/api-reference.md` (via image content)
- Image contains: Auth endpoint, token validation, protected resource flow

### Query: "configuration options" or "server port"
- Should match: `docs/configuration.html` (via image content)
- Image contains: server.port, database.url, cache.ttl settings

### Query: "error codes" or "429 too many requests"
- Should match: `docs/troubleshooting.htmx` (via image content)
- Image contains: HTTP status codes 400-503 with descriptions

## Edge Case Expected Behaviors

| File | Expected Behavior |
|------|-------------------|
| missing-image.md | Skip missing image, log warning, index text |
| remote-url.md | Skip http/https URLs, log warning, process local image |
| unsupported-format.md | Skip BMP, log warning, process PNG |
| text-looks-like-image.md | Only extract valid `![](path)` syntax |
| mixed-valid-invalid.md | Process 4 valid, skip 3 invalid, log 3 warnings |

## Usage

This repo is used as a git submodule in code-indexer for E2E testing:

```bash
# From code-indexer root
git submodule update --init test-fixtures/multimodal-mock-repo

# Index the test fixtures
cidx init test-fixtures/multimodal-mock-repo
cidx index test-fixtures/multimodal-mock-repo

# Run test queries
cidx query "database schema" --path test-fixtures/multimodal-mock-repo
```

## Image Generation

Images were generated using Python Pillow with predictable, searchable content.
See `generate_images.py` for regeneration if needed.
# Test change $(date)
# Test change Fri Jan 30 09:47:48 AM CST 2026
# Test change Fri Jan 30 09:47:58 AM CST 2026
