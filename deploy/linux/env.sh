# Sourced by the scripts in this folder. Reads KEY=value files the way systemd's EnvironmentFile
# does (values may contain spaces, optional surrounding quotes), which `. .env` would not:
# an unquoted OWNER_NAME=Roger Christopher would run "Christopher" as a command.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

load_env() {
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in '' | '#'*) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ ${#val} -ge 2 && ( "$val" == \"*\" || "$val" == \'*\' ) ]]; then
      val="${val:1:${#val}-2}"
    fi
    export "$key=$val"
  done <"$1"
}

# The same environment the systemd units get: .env first, then the native overrides.
load_service_env() {
  load_env "$REPO/.env"
  load_env "$REPO/deploy/linux/native.env"
}
