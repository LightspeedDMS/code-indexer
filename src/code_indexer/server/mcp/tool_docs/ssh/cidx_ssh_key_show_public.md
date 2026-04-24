---
name: cidx_ssh_key_show_public
category: ssh
required_permission: activate_repos
tl_dr: Get public key content for copying to remote servers.
---

TL;DR: Get public key content for copying to remote servers. WHEN TO USE: (1) Upload public key to GitHub/GitLab, (2) Add to authorized_keys on remote server, (3) Share public key with team member. WHEN NOT TO USE: Need full key metadata -> use cidx_ssh_key_list. OUTPUT: Returns formatted public key string suitable for direct copy/paste to authorized_keys or git hosting services. SECURITY: Only returns PUBLIC key (safe to share). Private key never exposed. RELATED TOOLS: cidx_ssh_key_list (view all keys), cidx_ssh_key_create (create new key), cidx_ssh_key_assign_host (configure SSH host). EXAMPLE: {"name": "github-key"} Returns: {"success": true, "name": "github-key", "public_key": "ssh-ed25519 AAAA...base64...== dev@example.com"}