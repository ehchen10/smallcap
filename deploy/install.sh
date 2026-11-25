#!/usr/bin/env bash
# Install smallcap watch as a systemd *user* service. Run from anywhere inside a clone.
# Generic Linux (tested target: Ubuntu): no root needed except the optional linger step.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-python3}"
"$PY" - <<'PYEOF' || { echo "python >= 3.10 required (set PYTHON=/path/to/python3.x)"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PYEOF

if ! "$PY" -m venv .venv 2>/dev/null; then
  echo "venv creation failed. On Ubuntu: sudo apt install python3-venv"; exit 1
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

if [ ! -f .env ]; then
  cp deploy/env.example .env
  echo "created .env — edit it and set SEC_USER_AGENT to your own contact info"
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|@REPO@|$REPO|g" deploy/smallcap-watch.service.in > "$UNIT_DIR/smallcap-watch.service"

cat <<MSG

Installed. Next:
  1. nano $REPO/.env                          # set SEC_USER_AGENT
  2. systemctl --user daemon-reload
  3. systemctl --user enable --now smallcap-watch
  4. sudo loginctl enable-linger "\$USER"      # keep it running after logout/reboot
  5. journalctl --user -u smallcap-watch -f   # watch it work
  6. cd $REPO && claude                       # then:  /loop follow STEERING.md
MSG
