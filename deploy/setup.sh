#!/usr/bin/env bash
# Mindees turnkey node provisioner -- idempotent, cloud-init friendly.
# Roles: validator (gossip+produce, public p2p), rpc (gossip + loopback JSON-RPC behind Caddy TLS).
#
# Usage (interactive):
#   sudo MINDEES_ROLE=validator \
#        MINDEES_ADVERTISE=seed1.mindees.example:9000 \
#        MINDEES_PEERS='seed2.mindees.example:9000 seed3.mindees.example:9000' \
#        bash deploy/setup.sh
#   sudo MINDEES_ROLE=rpc \
#        MINDEES_ADVERTISE=rpc.mindees.example:9000 \
#        MINDEES_PEERS='seed1.mindees.example:9000 seed2.mindees.example:9000 seed3.mindees.example:9000' \
#        MINDEES_RPC_DOMAIN=rpc.mindees.example \
#        bash deploy/setup.sh
# Then drop genesis.json + keystore + secrets (see GO-LIVE.md) and `systemctl enable --now`.
set -euo pipefail

REPO_URL="${MINDEES_REPO_URL:-https://github.com/aashir-athar/mindees-blockchain.git}"
REPO_REF="${MINDEES_REPO_REF:-main}"
APP_DIR="${MINDEES_APP_DIR:-/opt/mindees}"
DATA_DIR="${MINDEES_DATA_DIR:-/var/lib/mindees/data}"
KEYS_DIR="${MINDEES_KEYS_DIR:-/var/lib/mindees/keys}"
ENV_FILE="${MINDEES_ENV_FILE:-/etc/mindees/mindees.env}"
ROLE="${MINDEES_ROLE:-validator}"          # validator | rpc
ADVERTISE="${MINDEES_ADVERTISE:?set MINDEES_ADVERTISE to your PUBLIC host:port, e.g. seed1.example:9000}"
PEERS="${MINDEES_PEERS:-}"                  # space-separated host:port seed peers
P2P_PORT="${MINDEES_P2P_PORT:-9000}"
SLOT="${MINDEES_SLOT:-2}"
RPC_DOMAIN="${MINDEES_RPC_DOMAIN:-}"        # only for ROLE=rpc; the public TLS hostname
RUN_USER="${MINDEES_USER:-mindees}"

echo "[mindees] role=$ROLE advertise=$ADVERTISE peers='$PEERS' app=$APP_DIR data=$DATA_DIR"

# --- packages (Debian/Ubuntu) --------------------------------------------- #
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends python3 python3-pip python3-venv git ca-certificates curl
fi

# --- service user (no login, no home shell) ------------------------------- #
if ! id "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/mindees --create-home "$RUN_USER"
fi

# --- clone or update repo ------------------------------------------------- #
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin "$REPO_REF"
  git -C "$APP_DIR" reset --hard "origin/$REPO_REF"
else
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$APP_DIR"
fi

# --- python deps (system pip; tiny dep surface: cryptography only) -------- #
python3 -m pip install --no-cache-dir --break-system-packages -r "$APP_DIR/requirements.txt" 2>/dev/null \
  || python3 -m pip install --no-cache-dir -r "$APP_DIR/requirements.txt"

# --- directories ---------------------------------------------------------- #
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$DATA_DIR" "$KEYS_DIR"
install -d -m 0755 "$(dirname "$ENV_FILE")"

# --- env file (created once; never clobber operator secrets) --------------- #
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  cat > "$ENV_FILE" <<EOF
# Mindees node environment -- EDIT ME, then chmod 600. DO NOT COMMIT.
# Unlock the encrypted validator keystore (required for ROLE=validator and the rpc gossip node):
MINDEES_PASSPHRASE=CHANGE-ME
# Public RPC write-auth token (REQUIRED on the rpc host; Caddy injects it on writes).
# Generate with:  openssl rand -hex 32
MINDEES_RPC_TOKEN=CHANGE-ME
EOF
  chmod 600 "$ENV_FILE"
  echo "[mindees] wrote $ENV_FILE -- edit it with your real passphrase/token before starting"
fi

# --- systemd unit: gossip/producer node (p2p.py) -------------------------- #
PEER_FLAGS=""
for p in $PEERS; do PEER_FLAGS="$PEER_FLAGS --peer $p"; done

cat > /etc/systemd/system/mindees-node@.service <<EOF
[Unit]
Description=Mindees gossip/producer node (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
# validator role unlocks a keystore; rpc role also runs gossip but with no validator key.
ExecStart=/usr/bin/python3 $APP_DIR/p2p.py serve \\
  --data $DATA_DIR \\
  --host 0.0.0.0 \\
  --port $P2P_PORT \\
  --advertise $ADVERTISE \\
  --slot $SLOT \\
  \$MINDEES_VALIDATOR_FLAGS${PEER_FLAGS}
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

# Wire the validator keystore flag into the env file for the validator role only.
if [ "$ROLE" = "validator" ] && ! grep -q '^MINDEES_VALIDATOR_FLAGS=' "$ENV_FILE"; then
  echo "MINDEES_VALIDATOR_FLAGS=--validator-keystore $KEYS_DIR/validator.json" >> "$ENV_FILE"
elif [ "$ROLE" = "rpc" ] && ! grep -q '^MINDEES_VALIDATOR_FLAGS=' "$ENV_FILE"; then
  echo "MINDEES_VALIDATOR_FLAGS=" >> "$ENV_FILE"   # rpc gossip node carries no validator key
fi

# --- rpc role: loopback JSON-RPC service + Caddy TLS proxy ----------------- #
if [ "$ROLE" = "rpc" ]; then
  cat > /etc/systemd/system/mindees-rpc.service <<EOF
[Unit]
Description=Mindees JSON-RPC (loopback; behind Caddy TLS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
# Binds loopback. MINDEES_RPC_TOKEN MUST be set in the env file so write methods are gated
# even though the bind is 127.0.0.1 (the loopback guard does NOT protect against the proxy).
ExecStart=/usr/bin/python3 $APP_DIR/node.py serve --data $DATA_DIR --host 127.0.0.1 --port 8645
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

  if command -v apt-get >/dev/null 2>&1 && ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -y && apt-get install -y caddy
  fi

  if [ -n "$RPC_DOMAIN" ]; then
    install -d /etc/caddy
    # Render Caddyfile with the token read at boot from the env file.
    RPC_TOKEN_VAL="$(grep -E '^MINDEES_RPC_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
    sed -e "s|{{DOMAIN}}|$RPC_DOMAIN|g" -e "s|{{TOKEN}}|$RPC_TOKEN_VAL|g" \
        "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
    systemctl enable caddy >/dev/null 2>&1 || true
    systemctl reload caddy 2>/dev/null || systemctl restart caddy || true
    echo "[mindees] Caddy configured for https://$RPC_DOMAIN -> 127.0.0.1:8645"
  fi
fi

systemctl daemon-reload
echo
echo "[mindees] setup complete. Next:"
echo "  1) edit $ENV_FILE (MINDEES_PASSPHRASE, MINDEES_RPC_TOKEN)"
echo "  2) copy genesis.json -> $DATA_DIR/genesis.json   (verify sha256 == published hash)"
if [ "$ROLE" = "validator" ]; then
  echo "  3) copy your validatorN.json -> $KEYS_DIR/validator.json (chown $RUN_USER, chmod 600)"
  echo "  4) systemctl enable --now mindees-node@validator"
else
  echo "  3) systemctl enable --now mindees-node@rpc mindees-rpc"
  echo "  4) ensure firewall: open 443 + $P2P_PORT, keep 8645 CLOSED to the world"
fi
