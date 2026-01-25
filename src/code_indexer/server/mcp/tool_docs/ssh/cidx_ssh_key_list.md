---
name: cidx_ssh_key_list
category: ssh
required_permission: activate_repos
tl_dr: List all SSH keys (CIDX-managed and unmanaged).
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    managed:
      type: array
      description: CIDX-managed keys with full metadata
      items:
        type: object
        properties:
          name:
            type: string
            description: Key name
          fingerprint:
            type: string
            description: SSH fingerprint (SHA256)
          key_type:
            type: string
            description: Key type (ed25519/rsa)
          hosts:
            type: array
            items:
              type: string
            description: Hostnames configured in SSH config
          email:
            type:
            - string
            - 'null'
            description: Email address
          description:
            type:
            - string
            - 'null'
            description: Key description
          is_imported:
            type: boolean
            description: Whether key was imported (not yet implemented)
    unmanaged:
      type: array
      description: Keys detected in ~/.ssh but not managed by CIDX (cannot be assigned to hosts or deleted via CIDX)
      items:
        type: object
        properties:
          name:
            type: string
            description: Filename without extension
          fingerprint:
            type: string
            description: SSH fingerprint (SHA256)
          private_path:
            type: string
            description: Full path to private key file
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: List all SSH keys (CIDX-managed and unmanaged). WHEN TO USE: (1) See available keys, (2) Check key fingerprints, (3) View which hosts are assigned to each key, (4) Discover unmanaged keys in ~/.ssh. WHEN NOT TO USE: Need public key content -> use cidx_ssh_key_show_public. KEY TYPES: Managed keys have metadata (email, description, hosts), unmanaged keys are detected in ~/.ssh but not managed by CIDX. RELATED TOOLS: cidx_ssh_key_create (create key), cidx_ssh_key_show_public (get public key), cidx_ssh_key_assign_host (assign to host). EXAMPLE: {} Returns: {"success": true, "managed": [{"name": "github-key", "fingerprint": "SHA256:abc...", "key_type": "ed25519", "hosts": ["github.com"]}], "unmanaged": [{"name": "id_rsa", "fingerprint": "SHA256:xyz..."}]}