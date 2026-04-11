#!/bin/bash
# roku.sh — Control Roku via ECP with auth handshake
# Usage: ./roku.sh <roku-ip> <command>
# Commands: mute, home, play, pause, up, down, left, right, select, back, pair

ROKU_IP="${1:-10.5.5.74}"
COMMAND="${2:-mute}"
TOKEN_FILE="$HOME/.roku_token_${ROKU_IP//./_}"
CLIENT_ID="linux-roku-remote"

# ── Helpers ────────────────────────────────────────────────────────────────────

log()  { echo "[roku] $*"; }
fail() { echo "[roku] ERROR: $*" >&2; exit 1; }

keypress() {
  local key="$1"
  local token
  token=$(cat "$TOKEN_FILE" 2>/dev/null)

  if [[ -n "$token" ]]; then
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
      -H "Authorization: Bearer $token" \
      "http://$ROKU_IP:8060/keypress/$key")

    if [[ "$status" == "200" ]]; then
      log "Sent: $key"
      return 0
    elif [[ "$status" == "403" ]]; then
      log "Token rejected — re-pairing..."
      rm -f "$TOKEN_FILE"
    else
      fail "Unexpected response $status for keypress/$key"
    fi
  fi

  # No token or token rejected — try without auth first
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "http://$ROKU_IP:8060/keypress/$key")

  if [[ "$status" == "200" ]]; then
    log "Sent: $key (no auth needed)"
    return 0
  elif [[ "$status" == "403" ]]; then
    log "Auth required — run: $0 $ROKU_IP pair"
    exit 1
  else
    fail "Unexpected response $status — is Roku at $ROKU_IP reachable?"
  fi
}

# ── Pairing ────────────────────────────────────────────────────────────────────

pair() {
  log "Starting ECP pairing with $ROKU_IP ..."

  # Step 1: request pairing
  local resp
  resp=$(curl -s -X POST "http://$ROKU_IP:8060/ecp-session" \
    -H "Content-Type: application/json" \
    -d "{\"client-id\":\"$CLIENT_ID\",\"device-auth\":true}")

  log "Response: $resp"

  # Step 2: prompt for PIN shown on TV
  echo ""
  read -rp "Enter the PIN shown on your Roku TV: " pin

  # Step 3: submit PIN
  local token_resp
  token_resp=$(curl -s -X POST "http://$ROKU_IP:8060/ecp-session" \
    -H "Content-Type: application/json" \
    -d "{\"client-id\":\"$CLIENT_ID\",\"pin\":\"$pin\"}")

  log "Token response: $token_resp"

  # Extract token (tries both quoted formats)
  local token
  token=$(echo "$token_resp" | grep -oP '"device-auth-token"\s*:\s*"\K[^"]+' \
    || echo "$token_resp" | grep -oP 'token["\s:]+\K\S+' | tr -d '",')

  if [[ -z "$token" ]]; then
    # Some Rokus just return 200 with no token — auth may not be required
    log "No token in response. Roku may not require auth — try sending commands directly."
    exit 0
  fi

  echo "$token" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  log "Paired! Token saved to $TOKEN_FILE"
}

# ── Main ───────────────────────────────────────────────────────────────────────

# Check Roku is reachable
curl -s --connect-timeout 2 "http://$ROKU_IP:8060/query/device-info" -o /dev/null \
  || fail "Cannot reach Roku at $ROKU_IP:8060 — check IP and network"

case "$COMMAND" in
  pair)         pair ;;
  mute)         keypress VolumeMute ;;
  home)         keypress Home ;;
  play|pause)   keypress Play ;;
  up)           keypress Up ;;
  down)         keypress Down ;;
  left)         keypress Left ;;
  right)        keypress Right ;;
  select|ok)    keypress Select ;;
  back)         keypress Back ;;
  fwd)          keypress Fwd ;;
  rev)          keypress Rev ;;
  volup)        keypress VolumeUp ;;
  voldown)      keypress VolumeDown ;;
  poweroff)     keypress PowerOff ;;
  info)
    curl -s "http://$ROKU_IP:8060/query/device-info" | grep -E 'friendly-device-name|model-name|software-version'
    ;;
  apps)
    curl -s "http://$ROKU_IP:8060/query/apps" | grep -oP '(?<=<name>)[^<]+'
    ;;
  key)
    # Send arbitrary keypress: ./roku.sh <ip> key <KeyName>
    keypress "${3:-Select}"
    ;;
  *)
    echo "Usage: $0 <roku-ip> <command>"
    echo ""
    echo "Commands:"
    echo "  pair          — Authenticate with Roku (run once if getting 403)"
    echo "  mute          — Toggle mute"
    echo "  home          — Home screen"
    echo "  play/pause    — Play/pause toggle"
    echo "  up/down/left/right/select/back"
    echo "  fwd/rev       — Fast forward / rewind"
    echo "  volup/voldown — Volume"
    echo "  poweroff      — Turn off"
    echo "  info          — Show device info"
    echo "  apps          — List installed apps"
    echo "  key <Name>    — Send any raw keypress"
    exit 1
    ;;
esac
