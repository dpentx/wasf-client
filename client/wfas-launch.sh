#!/usr/bin/env bash
set -uo pipefail

INSTALL_DIR="$HOME/wfas-client-native"
LOG_FILE="$HOME/.cache/wfas-native-client.log"
mkdir -p "$HOME/.cache"

start_via_systemd() {
    systemctl --user start wfas-native-client.service >/dev/null 2>&1
}

start_directly() {
    if ! pgrep -f "wfas_native_client.py" >/dev/null 2>&1; then
        nohup python3 "$INSTALL_DIR/wfas_native_client.py" >>"$LOG_FILE" 2>&1 &
        disown
    fi
}

if command -v systemctl >/dev/null 2>&1 && systemctl --user show wfas-native-client.service >/dev/null 2>&1; then
    start_via_systemd
else
    start_directly
fi

sleep 1

exec python3 "$INSTALL_DIR/wfas_tui.py"
