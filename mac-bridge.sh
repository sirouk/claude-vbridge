#!/bin/bash
# Portable macOS operator launcher. Machine settings stay in ~/.vbridge.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${VBRIDGE_HOME:-$HOME/.vbridge}"
PLIST="$HOME/Library/LaunchAgents/com.vbridge.server.plist"
SERVICE="gui/$(id -u)/com.vbridge.server"
PYTHON="$ROOT/.venv/bin/python"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
usage() {
  printf '%s\n' 'VBridge — direct Claude connector to this Mac'     'Usage: ./mac-bridge.sh [install|start|restart|status|check|permissions|settings|connector|logs|stop|help]'     'Install: ./mac-bridge.sh install --public-url https://YOUR-HOSTNAME'     'No argument = start. macOS permissions and Claude connector setup remain manual.'
}
require_python() {
  if [[ ! -x "$PYTHON" ]]; then
    printf '%s\n' 'Missing project environment. Run uv sync --locked first.' >&2
    exit 1
  fi
}
admin() { require_python; "$PYTHON" "$ROOT/scripts/bridge_admin.py" "$@"; }
require_install() {
  require_python
  if [[ ! -f "$PLIST" ]]; then
    printf '%s\n' 'Missing LaunchAgent. Run ./mac-bridge.sh install --public-url https://YOUR-HOSTNAME' >&2
    exit 1
  fi
}
start_bridge() {
  require_install
  admin enable
  if ! launchctl print "$SERVICE" >/dev/null 2>&1; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
  fi
  admin ready
  admin routes
  printf '\nConnector URL: %s\n' "$(admin url)"
  printf '%s\n' 'Name: VBridge | Sign in now | Register automatically | No request headers'     'Run ./mac-bridge.sh check to verify OAuth and direct local tools.'
}
case "${1:-start}" in
  install)
    shift
    require_python
    admin install "$@"
    ;;
  start) start_bridge ;;
  restart)
    require_install
    admin enable
    if launchctl print "$SERVICE" >/dev/null 2>&1; then
      launchctl kickstart -k "$SERVICE"
    else
      launchctl bootstrap "gui/$(id -u)" "$PLIST"
    fi
    admin ready
    admin routes
    printf 'Restarted. Connector URL: %s\n' "$(admin url)"
    ;;
  status)
    if [[ -f "$STATE/DISABLED" ]]; then printf '%s\n' 'Bridge: DISABLED'; fi
    if launchctl print "$SERVICE" >/dev/null 2>&1; then
      printf '%s\n' 'LaunchAgent: loaded'
    else printf '%s\n' 'LaunchAgent: not loaded'; fi
    if ! curl --fail --silent --show-error --max-time 5 "$(admin local-url)"; then
      printf '\nLocal bridge is not ready.\n' >&2; exit 1
    fi
    printf '\nConnector URL: %s\n' "$(admin url)"
    printf '%s\n' 'For private routing details run tailscale funnel status locally.'
    ;;
  check|permissions)
    require_install
    admin "${1}"
    ;;
  settings)
    require_python
    open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
    open -R "$(admin runtime)"
    printf '%s\n' 'Accessibility settings opened. Python is selected in Finder.'       'Full Disk Access and Screen Recording are also under Privacy & Security. See RUNBOOK.md.'
    ;;
  connector)
    printf 'Name: VBridge\nURL: %s\nAuthentication: Sign in now\nOAuth client: Register automatically\nRequest headers: empty\n' "$(admin url)"
    printf 'Private passphrase: %s/passphrase (read locally; do not paste into shared logs).\n' "$STATE"
    ;;
  logs) tail -n 80 -F "$STATE/server.log" ;;
  stop)
    admin disable
    if launchctl print "$SERVICE" >/dev/null 2>&1; then launchctl bootout "$SERVICE"; fi
    printf '%s\n' 'Bridge disabled and stopped. Existing Funnel routes are untouched.'       'The kill switch remains effective after login. Run ./mac-bridge.sh start to re-enable.'
    ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
