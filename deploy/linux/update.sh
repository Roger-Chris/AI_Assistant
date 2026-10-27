#!/usr/bin/env bash
# Pull the latest code and re-run the installer (dependencies, units, restart).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

git -C "$REPO" pull --ff-only
exec "$REPO/deploy/linux/install.sh"
