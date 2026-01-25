---
name: cidx_ssh_key_create
category: ssh
required_permission: activate_repos
tl_dr: Create new SSH key pair managed by CIDX server.
inputSchema:
  type: object
  properties:
    name:
      type: string
      description: 'Key name (identifier). Must be filesystem-safe (alphanumeric, dashes, underscores). Used for filenames:
        ~/.ssh/cidx-managed-{name}'
    key_type:
      type: string
      enum:
      - ed25519
      - rsa
      default: ed25519
      description: 'Key type to generate. ed25519: Modern, secure, fast (default). rsa: 4096-bit, wider compatibility with
        older systems.'
    email:
      type: string
      description: Email address for key comment (appears in public key). Optional but recommended for key identification
        on remote servers.
    description:
      type: string
      description: 'Human-readable description of key purpose. Example: ''GitHub personal repos'' or ''Production server access'''
  required:
  - name
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether key creation succeeded
    name:
      type: string
      description: Key name (identifier)
    fingerprint:
      type: string
      description: SSH key fingerprint (SHA256)
    key_type:
      type: string
      description: Key type (ed25519/rsa)
    public_key:
      type: string
      description: Full public key content (suitable for copying to authorized_keys or git hosting services)
    email:
      type:
      - string
      - 'null'
      description: Email address (if provided)
    description:
      type:
      - string
      - 'null'
      description: Key description (if provided)
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Create new SSH key pair managed by CIDX server. WHEN TO USE: (1) Generate SSH key for remote repository access, (2) Create key with specific type (ed25519/rsa), (3) Generate key with email/description metadata. WHEN NOT TO USE: Key already exists with that name -> delete first | Need to import existing key -> not yet supported. SECURITY: Keys stored in ~/.ssh/ with metadata in ~/.code-indexer-server/ssh_keys/. Generated keys are 4096-bit RSA or Ed25519 (default). RELATED TOOLS: cidx_ssh_key_list (view keys), cidx_ssh_key_assign_host (configure SSH host), cidx_ssh_key_show_public (get public key for server upload). EXAMPLE: {"name": "github-key", "key_type": "ed25519", "email": "dev@example.com"} Returns: {"success": true, "name": "github-key", "fingerprint": "SHA256:abc123...", "key_type": "ed25519", "public_key": "ssh-ed25519 AAAA... dev@example.com"}