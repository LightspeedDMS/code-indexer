---
name: cidx_ssh_key_list
category: ssh
required_permission: activate_repos
tl_dr: List all SSH keys (CIDX-managed and unmanaged).
---

TL;DR: List all SSH keys (CIDX-managed and unmanaged). WHEN TO USE: (1) See available keys, (2) Check key fingerprints, (3) View which hosts are assigned to each key, (4) Discover unmanaged keys in ~/.ssh. WHEN NOT TO USE: Need public key content -> use cidx_ssh_key_show_public. KEY TYPES: Managed keys have metadata (email, description, hosts), unmanaged keys are detected in ~/.ssh but not managed by CIDX. RELATED TOOLS: cidx_ssh_key_create (create key), cidx_ssh_key_show_public (get public key), cidx_ssh_key_assign_host (assign to host). EXAMPLE: {} Returns: {"success": true, "managed": [{"name": "github-key", "fingerprint": "SHA256:abc...", "key_type": "ed25519", "hosts": ["github.com"]}], "unmanaged": [{"name": "id_rsa", "fingerprint": "SHA256:xyz..."}]}