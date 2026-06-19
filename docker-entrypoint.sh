#!/bin/sh
# Initialise genesis on first boot (idempotent), then run the gossiping, self-producing node.
# All nodes that share MINDEES_GENESIS_TO / _STAKE derive a byte-identical genesis and agree.
set -e

DATA="${MINDEES_DATA:-/data}"

if [ ! -f "$DATA/genesis.json" ]; then
  python node.py init --data "$DATA" \
    --to "${MINDEES_GENESIS_TO:?set MINDEES_GENESIS_TO to the validator address}" \
    --stake "${MINDEES_GENESIS_STAKE:-100000}"
fi

PEER_ARGS=""
for p in $MINDEES_PEERS; do PEER_ARGS="$PEER_ARGS --peer $p"; done

VALIDATOR_ARG=""
if [ -n "$MINDEES_VALIDATOR_SECRET" ]; then
  VALIDATOR_ARG="--validator-secret $MINDEES_VALIDATOR_SECRET"
fi

# shellcheck disable=SC2086
exec python p2p.py serve \
  --data "$DATA" \
  --port "${MINDEES_PORT:-9000}" \
  --slot "${MINDEES_SLOT:-2}" \
  $VALIDATOR_ARG $PEER_ARGS
