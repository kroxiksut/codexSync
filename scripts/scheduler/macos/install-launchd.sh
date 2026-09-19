#!/usr/bin/env bash
set -euo pipefail

# DEPRECATED: launchd.config.sh is a second source of scheduler settings next to
# config.toml. Kept working for existing installs; see docs/en/CONFIGURATION.md.
echo "WARNING: DEPRECATED: install-launchd.sh and launchd.config.sh will be removed in a later release." >&2
echo "WARNING: Set [scheduler] in config.toml and run 'codexsync -c <config.toml> automation apply' instead." >&2
echo "WARNING: If you continue, remove this agent first (uninstall-launchd.sh) before using automation apply, so two jobs do not run." >&2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${1:-$SCRIPT_DIR/launchd.config.sh}"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Config file not found: $CONFIG_PATH" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_PATH"

RUNNER_SCRIPT="$SCRIPT_DIR/run-codexsync.sh"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"

for required in LABEL PYTHON_BIN PROJECT_DIR CONFIG_FILE MODE INTERVAL_SECONDS STDOUT_LOG STDERR_LOG; do
  if [[ -z "${!required:-}" ]]; then
    echo "Required setting is empty: $required" >&2
    exit 1
  fi
done

if [[ "$MODE" != "dry-run" && "$MODE" != "apply" ]]; then
  echo "MODE must be dry-run or apply" >&2
  exit 1
fi

if ! [[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || [[ "$INTERVAL_SECONDS" -lt 60 ]]; then
  echo "INTERVAL_SECONDS must be integer >= 60" >&2
  exit 1
fi

if [[ ! -x "$RUNNER_SCRIPT" ]]; then
  chmod +x "$RUNNER_SCRIPT"
fi

mkdir -p "$(dirname "$PLIST_PATH")"
mkdir -p "$(dirname "$STDOUT_LOG")"
mkdir -p "$(dirname "$STDERR_LOG")"

run_at_load_tag="<false/>"
if [[ "${RUN_AT_LOAD:-true}" == "true" ]]; then
  run_at_load_tag="<true/>"
fi

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>${RUNNER_SCRIPT}</string>
      <string>${PYTHON_BIN}</string>
      <string>${PROJECT_DIR}</string>
      <string>${CONFIG_FILE}</string>
      <string>${MODE}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>StartInterval</key>
    <integer>${INTERVAL_SECONDS}</integer>
    <key>RunAtLoad</key>
    ${run_at_load_tag}
    <key>StandardOutPath</key>
    <string>${STDOUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${STDERR_LOG}</string>
  </dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "LaunchAgent installed: ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Interval (seconds): ${INTERVAL_SECONDS}"
echo "Mode: ${MODE}"
echo "To uninstall: ./uninstall-launchd.sh \"$CONFIG_PATH\""
