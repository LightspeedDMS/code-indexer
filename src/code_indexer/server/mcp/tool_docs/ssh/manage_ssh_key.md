---
name: manage_ssh_key
category: ssh
required_permission: repository:admin
tl_dr: Create, delete, show public key, or assign host for a CIDX-managed SSH key.
slim_description: "Unified SSH key management: create new key pair, delete key, retrieve public key content, or assign key to an SSH hostname."
inputSchema:
  type: object
  properties:
    action:
      type: string
      enum:
      - create
      - delete
      - show_public
      - assign_host
      description: 'Operation to perform. create: generate new key pair. delete: remove managed key. show_public: get public key content. assign_host: add SSH config Host entry.'
    name:
      type: string
      description: Key name (identifier). Required for all actions.
    key_type:
      type: string
      enum:
      - ed25519
      - rsa
      default: ed25519
      description: 'Key type to generate (create only). ed25519: modern and fast (default). rsa: 4096-bit, wider compatibility.'
    email:
      type: string
      description: Email address for key comment (create only). Optional but recommended.
    description:
      type: string
      description: Human-readable description of key purpose (create only). Optional.
    hostname:
      type: string
      description: 'Hostname or Host alias for SSH config (assign_host only). Examples: github.com, gitlab.com, myserver.example.com.'
    force:
      type: boolean
      default: false
      description: 'Force overwrite if hostname already exists in SSH config (assign_host only). Default: false.'
  required:
  - action
  - name
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
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
      description: Full public key content (create and show_public actions)
    hosts:
      type: array
      items:
        type: string
      description: All hostnames configured for this key (assign_host action)
    message:
      type: string
      description: Human-readable result message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Unified SSH key management (Story #992). Replaces cidx_ssh_key_create, cidx_ssh_key_delete, cidx_ssh_key_show_public, cidx_ssh_key_assign_host.

ACTIONS:
- create: Generate new SSH key pair. Required: name. Optional: key_type (ed25519/rsa), email, description.
- delete: Remove a managed SSH key. Required: name.
- show_public: Get public key content for copy/paste. Required: name.
- assign_host: Add SSH config Host entry. Required: name, hostname. Optional: force (overwrite conflict).

RELATED TOOLS: list_ssh_keys (view all keys).

EXAMPLES:
- Create: {"action": "create", "name": "github-key", "key_type": "ed25519", "email": "dev@example.com"}
- Delete: {"action": "delete", "name": "old-key"}
- Show public: {"action": "show_public", "name": "github-key"}
- Assign host: {"action": "assign_host", "name": "github-key", "hostname": "github.com"}
