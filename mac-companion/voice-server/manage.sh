#!/usr/bin/env bash
set -euo pipefail

LABEL="com.thinksmart.voice"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/xiaozhi-voice"
UID_NUM="$(id -u)"

cmd="${1:-status}"

case "$cmd" in
  start)
    mkdir -p "$LOG_DIR"
    launchctl bootstrap "gui/${UID_NUM}" "$PLIST" 2>/dev/null || launchctl kickstart -k "gui/${UID_NUM}/${LABEL}"
    echo "Started ${LABEL}"
    ;;
  stop)
    launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
    echo "Stopped ${LABEL}"
    ;;
  restart)
    mkdir -p "$LOG_DIR"
    if launchctl print "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1; then
      launchctl kickstart -k "gui/${UID_NUM}/${LABEL}"
    else
      launchctl bootstrap "gui/${UID_NUM}" "$PLIST"
    fi
    echo "Restarted ${LABEL}"
    ;;
  status)
    curl -s http://127.0.0.1:8010/health || echo "Service not responding on :8010"
    echo ""
    ;;
  logs)
    tail -n 50 "$LOG_DIR/server.out.log" "$LOG_DIR/server.err.log" 2>/dev/null
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
