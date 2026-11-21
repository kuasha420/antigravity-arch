#!/usr/bin/env bash
set -euo pipefail

XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
FLAGS_FILE="$XDG_CONFIG_HOME/antigravity-ide-flags.conf"

declare -a USER_FLAGS=()
if [[ -f "$FLAGS_FILE" ]]; then
    while IFS= read -r line; do
        line="${line%%#*}"
        for token in $line; do
            USER_FLAGS+=("$token")
        done
    done < "$FLAGS_FILE"
fi

exec /opt/antigravity-ide/bin/antigravity-ide "$@" "${USER_FLAGS[@]}"
