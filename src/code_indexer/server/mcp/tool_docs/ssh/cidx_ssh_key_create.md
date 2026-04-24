---
name: cidx_ssh_key_create
category: ssh
required_permission: activate_repos
tl_dr: Create new SSH key pair managed by CIDX server.
---

TL;DR: Create new SSH key pair managed by CIDX server. WHEN TO USE: (1) Generate SSH key for remote repository access, (2) Create key with specific type (ed25519/rsa), (3) Generate key with email/description metadata. WHEN NOT TO USE: Key already exists with that name -> delete first | Need to import existing key -> not yet supported. SECURITY: Keys stored in ~/.ssh/ with metadata in ~/.code-indexer-server/ssh_keys/. Generated keys are 4096-bit RSA or Ed25519 (default). RELATED TOOLS: cidx_ssh_key_list (view keys), cidx_ssh_key_assign_host (configure SSH host), cidx_ssh_key_show_public (get public key for server upload). EXAMPLE: {"name": "github-key", "key_type": "ed25519", "email": "dev@example.com"} Returns: {"success": true, "name": "github-key", "fingerprint": "SHA256:abc123...", "key_type": "ed25519", "public_key": "ssh-ed25519 AAAA... dev@example.com"}