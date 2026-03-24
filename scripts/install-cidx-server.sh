#!/bin/bash
# install-cidx-server.sh — Install CIDX server on a fresh machine
#
# Idempotent: safe to re-run. Handles Rocky Linux / RHEL / Ubuntu.
#
# Usage:
#   ./install-cidx-server.sh [--branch BRANCH] [--voyage-key KEY] [--port PORT]
#
# Prerequisites: SSH access, sudo privileges
#
# What it does:
#   1. Installs system packages (git, python3-pip, nfs-utils, gcc, etc.)
#   2. Clones code-indexer repo (or pulls if already cloned)
#   3. Installs Python dependencies
#   4. Creates ~/.cidx-server data directory
#   5. Creates and enables systemd service
#   6. Starts the server

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

REPO_URL="https://github.com/LightspeedDMS/code-indexer.git"
BRANCH="epic/408-cidx-clusterization"
INSTALL_DIR="${HOME}/code-indexer"
DATA_DIR="${HOME}/.cidx-server"
PORT=8000
VOYAGE_KEY=""
PYTHON="python3"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --voyage-key) VOYAGE_KEY="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --repo-url) REPO_URL="$2"; shift 2 ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [--branch BRANCH] [--voyage-key KEY] [--port PORT]"
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Detect package manager
# ---------------------------------------------------------------------------

if command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
    PKG_INSTALL="sudo dnf install -y"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
    PKG_INSTALL="sudo yum install -y"
elif command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
    PKG_INSTALL="sudo apt-get install -y"
else
    echo "ERROR: No supported package manager found (dnf/yum/apt)"
    exit 1
fi

echo "=== CIDX Server Installation ==="
echo "  Package manager: $PKG_MGR"
echo "  Branch: $BRANCH"
echo "  Install dir: $INSTALL_DIR"
echo "  Data dir: $DATA_DIR"
echo "  Port: $PORT"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Install system dependencies
# ---------------------------------------------------------------------------

echo "--- Step 1: System dependencies ---"

PACKAGES="git nfs-utils gcc"

if [[ "$PKG_MGR" == "apt" ]]; then
    PACKAGES="git nfs-common gcc python3-pip python3-dev libpq-dev"
    sudo apt-get update -qq
else
    # RHEL/Rocky
    PACKAGES="git nfs-utils gcc python3-pip python3-devel"
    # Enable CRB/PowerTools for development headers
    sudo dnf install -y epel-release 2>/dev/null || true
    sudo dnf config-manager --set-enabled crb 2>/dev/null || true
fi

$PKG_INSTALL $PACKAGES
echo "System packages installed."

# Ensure pip is up to date
$PYTHON -m pip install --upgrade pip 2>/dev/null || true

echo ""

# ---------------------------------------------------------------------------
# Step 2: Clone or update repository
# ---------------------------------------------------------------------------

echo "--- Step 2: Clone/update repository ---"

if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "Repository exists at $INSTALL_DIR, pulling latest..."
    cd "$INSTALL_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
else
    echo "Cloning repository..."
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo "Repository ready at $INSTALL_DIR (branch: $(git branch --show-current))"
echo ""

# ---------------------------------------------------------------------------
# Step 3: Install Python dependencies
# ---------------------------------------------------------------------------

echo "--- Step 3: Python dependencies ---"

cd "$INSTALL_DIR"

# Install the package in editable mode
$PYTHON -m pip install -e . 2>&1 | tail -5

# Install cluster-specific dependencies
$PYTHON -m pip install psycopg psycopg-pool requests 2>&1 | tail -3

# Verify critical imports
$PYTHON -c "import code_indexer; print(f'code-indexer v{code_indexer.__version__} installed')"
$PYTHON -c "import psycopg; print(f'psycopg v{psycopg.__version__} installed')"
$PYTHON -c "import psycopg_pool; print('psycopg-pool installed')"

echo ""

# ---------------------------------------------------------------------------
# Step 4: Create data directory
# ---------------------------------------------------------------------------

echo "--- Step 4: Data directory ---"

mkdir -p "$DATA_DIR/data/golden-repos"
mkdir -p "$DATA_DIR/logs"
mkdir -p "$DATA_DIR/locks"

# Create default config if none exists
if [[ ! -f "$DATA_DIR/config.json" ]]; then
    cat > "$DATA_DIR/config.json" << CONFIGEOF
{
  "host": "0.0.0.0",
  "port": $PORT,
  "log_level": "INFO",
  "storage_mode": "sqlite"
}
CONFIGEOF
    echo "Created default config.json"
else
    echo "config.json already exists, not overwriting"
fi

echo "Data directory ready at $DATA_DIR"
echo ""

# ---------------------------------------------------------------------------
# Step 5: Create systemd service
# ---------------------------------------------------------------------------

echo "--- Step 5: Systemd service ---"

SERVICE_FILE="/etc/systemd/system/cidx-server.service"

if [[ -f "$SERVICE_FILE" ]]; then
    echo "Systemd service already exists, updating..."
fi

sudo tee "$SERVICE_FILE" > /dev/null << SERVICEEOF
[Unit]
Description=CIDX Server - Code Indexer Server with Semantic Search
Documentation=https://github.com/LightspeedDMS/code-indexer
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$INSTALL_DIR

Environment="PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"
Environment="PYTHONPATH=$INSTALL_DIR/src"
Environment="CIDX_SERVER_MODE=1"
Environment="CIDX_ISSUER_URL=http://localhost:$PORT"
Environment="CIDX_REPO_ROOT=$INSTALL_DIR"
$(if [[ -n "$VOYAGE_KEY" ]]; then echo "Environment=\"VOYAGE_API_KEY=$VOYAGE_KEY\""; fi)

ExecStart=$PYTHON -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port $PORT --log-level info --workers 1

Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=cidx-server

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable cidx-server

echo "Systemd service created and enabled"
echo ""

# ---------------------------------------------------------------------------
# Step 6: Start server
# ---------------------------------------------------------------------------

echo "--- Step 6: Starting server ---"

sudo systemctl restart cidx-server
sleep 5

if systemctl is-active --quiet cidx-server; then
    echo "CIDX server is running!"
    echo ""
    echo "=== Installation Complete ==="
    echo "  Server: http://localhost:$PORT"
    echo "  Status: systemctl status cidx-server"
    echo "  Logs:   journalctl -u cidx-server -f"
    echo "  Config: $DATA_DIR/config.json"
    echo ""
    echo "Next steps:"
    echo "  1. Set admin password (first login creates admin user)"
    echo "  2. To join a cluster: ./scripts/cluster-join.sh --help"
else
    echo "WARNING: Server failed to start. Check logs:"
    echo "  journalctl -u cidx-server --no-pager -n 30"
    exit 1
fi
