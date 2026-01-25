---
name: cidx_ssh_key_show_public
category: ssh
required_permission: activate_repos
tl_dr: Get public key content for copying to remote servers.
inputSchema:
  type: object
  properties:
    name:
      type: string
      description: Key name to retrieve public key for
  required:
  - name
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    name:
      type: string
      description: Key name
    public_key:
      type: string
      description: 'Full public key content (suitable for copying to authorized_keys or git hosting services). Format: ''ssh-ed25519
        AAAA... user@host'' or ''ssh-rsa AAAA... user@host'''
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Get public key content for copying to remote servers. WHEN TO USE: (1) Upload public key to GitHub/GitLab, (2) Add to authorized_keys on remote server, (3) Share public key with team member. WHEN NOT TO USE: Need full key metadata -> use cidx_ssh_key_list. OUTPUT: Returns formatted public key string suitable for direct copy/paste to authorized_keys or git hosting services. SECURITY: Only returns PUBLIC key (safe to share). Private key never exposed. RELATED TOOLS: cidx_ssh_key_list (view all keys), cidx_ssh_key_create (create new key), cidx_ssh_key_assign_host (configure SSH host). EXAMPLE: {"name": "github-key"} Returns: {"success": true, "name": "github-key", "public_key": "ssh-ed25519 AAAA...base64...== dev@example.com"}