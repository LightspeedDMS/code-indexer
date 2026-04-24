---
name: cidx_ssh_key_assign_host
category: ssh
required_permission: activate_repos
tl_dr: Assign SSH key to hostname in SSH config (~/.
inputSchema:
  type: object
  properties:
    name:
      type: string
      description: Key name to assign
    hostname:
      type: string
      description: 'Hostname or Host alias for SSH config. Examples: ''github.com'', ''gitlab.com'', ''myserver.example.com'',
        ''production-server'''
    force:
      type: boolean
      default: false
      description: 'Force overwrite if hostname already exists in SSH config. Default: false (fails on conflict). Set to true
        to replace existing Host entry.'
  required:
  - name
  - hostname
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    name:
      type: string
      description: Key name
    fingerprint:
      type: string
      description: SSH key fingerprint (SHA256)
    key_type:
      type: string
      description: Key type (ed25519/rsa)
    hosts:
      type: array
      items:
        type: string
      description: All hostnames now configured for this key
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
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Assign SSH key to hostname in SSH config (~/.ssh/config). WHEN TO USE: (1) Configure key for GitHub/GitLab host, (2) Set up key for remote server access, (3) Create SSH Host entry for repository cloning. WHEN NOT TO USE: Host already configured -> use force=true to override. CONFIGURATION: Adds 'Host {hostname}' entry to SSH config with IdentityFile pointing to the managed key. Updates ~/.ssh/config with proper formatting and preserves existing configuration. CONFLICT HANDLING: By default, fails if hostname already exists in SSH config. Use force=true to replace existing Host entry. RELATED TOOLS: cidx_ssh_key_create (create key first), cidx_ssh_key_list (view configured hosts), cidx_ssh_key_show_public (get public key for remote server setup). EXAMPLE: {"name": "github-key", "hostname": "github.com"} Returns: {"success": true, "name": "github-key", "hostname": "github.com", "message": "Host github.com configured to use key github-key"}