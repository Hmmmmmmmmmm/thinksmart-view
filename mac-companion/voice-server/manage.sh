#!/usr/bin/env bash
set -euo pipefail

UID_NUM="$(id -u)"
VOICE_LOG_DIR="$HOME/Library/Logs/xiaozhi-voice"
VISION_LOG_DIR="$HOME/Library/Logs/study-vision"
GATEWAY_PORT="${GATEWAY_PORT:-8080}"
VISION_PORT="${STUDY_VISION_PORT:-8084}"

cmd="${1:-status}"
target="${2:-}"

label_for() {
  case "$1" in
    voice)   echo "com.thinksmart.voice" ;;
    tts2)    echo "com.thinksmart.voice-tts2" ;;
    gateway) echo "com.thinksmart.gateway" ;;
    vision)  echo "com.thinksmart.study-vision-bridge" ;;
    *)       echo "Unknown service: $1 (expected voice|tts2|gateway|vision|all)" >&2; exit 1 ;;
  esac
}

targets_for() {
  local default_t="$1"
  local t="${target:-$default_t}"
  if [ "$t" = "all" ]; then
    echo "voice tts2 gateway vision"
  else
    echo "$t"
  fi
}

case "$cmd" in
  start)
    mkdir -p "$VOICE_LOG_DIR" "$VISION_LOG_DIR"
    for s in $(targets_for voice); do
      lbl="$(label_for "$s")"
      plist="$HOME/Library/LaunchAgents/${lbl}.plist"
      launchctl bootstrap "gui/${UID_NUM}" "$plist" 2>/dev/null || launchctl kickstart -k "gui/${UID_NUM}/${lbl}"
      echo "Started ${lbl}"
    done
    ;;
  stop)
    for s in $(targets_for voice); do
      lbl="$(label_for "$s")"
      launchctl bootout "gui/${UID_NUM}/${lbl}" 2>/dev/null || true
      echo "Stopped ${lbl}"
    done
    ;;
  restart)
    mkdir -p "$VOICE_LOG_DIR" "$VISION_LOG_DIR"
    for s in $(targets_for voice); do
      lbl="$(label_for "$s")"
      plist="$HOME/Library/LaunchAgents/${lbl}.plist"
      if launchctl print "gui/${UID_NUM}/${lbl}" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/${UID_NUM}/${lbl}"
      else
        launchctl bootstrap "gui/${UID_NUM}" "$plist"
      fi
      echo "Restarted ${lbl}"
    done
    ;;
  status)
    for s in $(targets_for all); do
      case "$s" in
        voice)
          printf "[voice   :8010 ] "
          curl -fsS --max-time 3 http://127.0.0.1:8010/health || printf "not responding"
          echo ""
          ;;
        tts2)
          printf "[tts2    :8011 ] "
          curl -fsS --max-time 3 http://127.0.0.1:8011/health || printf "not responding"
          echo ""
          ;;
        gateway)
          printf "[gateway :%s ] " "$GATEWAY_PORT"
          curl -fsS --max-time 3 "http://127.0.0.1:${GATEWAY_PORT}/health" || printf "not responding"
          echo ""
          ;;
        vision)
          printf "[vision  :%s ] " "$VISION_PORT"
          curl -fsS --max-time 3 "http://127.0.0.1:${VISION_PORT}/health" || printf "not responding"
          echo ""
          ;;
        *)
          label_for "$s" >/dev/null
          ;;
      esac
    done
    ;;
  logs)
    case "${target:-voice}" in
      voice)   tail -n 50 "$VOICE_LOG_DIR/server.out.log" "$VOICE_LOG_DIR/server.err.log" 2>/dev/null ;;
      tts2)    tail -n 50 "$VOICE_LOG_DIR/tts2.out.log" "$VOICE_LOG_DIR/tts2.err.log" 2>/dev/null ;;
      gateway) tail -n 50 "$VOICE_LOG_DIR/gateway.out.log" "$VOICE_LOG_DIR/gateway.err.log" 2>/dev/null ;;
      vision)  tail -n 50 "$VISION_LOG_DIR/bridge.out.log" "$VISION_LOG_DIR/bridge.err.log" 2>/dev/null ;;
      all)     tail -n 25 "$VOICE_LOG_DIR/"*.log "$VISION_LOG_DIR/bridge."*.log 2>/dev/null ;;
      *)       label_for "$target" >/dev/null ;;
    esac
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs} [voice|tts2|gateway|vision|all]"
    exit 1
    ;;
esac
