#!/bin/bash
umask 077
# Connect directly via SSH -N -T first (lowest latency, zero proxy hairpin, works even if local proxy is stopped),
# with automatic failover to local SOCKS5 proxy if direct connection fails quickly.
# Forwards both Intercom WebSocket (:18091 -> 127.0.0.1:18091) and Device SSH (:22 -> 127.0.0.1:28022).
REMOTE_HOST="${INTERCOM_REMOTE_HOST:-root@203.0.113.10}"
REMOTE_PORT="${INTERCOM_REMOTE_PORT:-22}"
SOCKS_PROXY="${INTERCOM_SOCKS_PROXY:-127.0.0.1:1080}"
SOCKS_PORT="${SOCKS_PROXY##*:}"
ERR_FILE="/run/thinksmart-intercom-tunnel.err"
UP_FILE="/run/thinksmart-intercom-tunnel.up"
WATCH_PID=""
BASE_OPTS=(
    -N -T
    -i /etc/thinksmart-intercom/id_ed25519
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile=/etc/thinksmart-intercom/known_hosts
    -o LogLevel=ERROR
    -o ConnectTimeout=8
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=2
    -o TCPKeepAlive=yes
    -o ExitOnForwardFailure=yes
    -p "$REMOTE_PORT"
    -R 127.0.0.1:18091:127.0.0.1:18091
    -R 127.0.0.1:28022:127.0.0.1:22
)

SSH_PID=""
cleanup() {
    if [ -n "$WATCH_PID" ] && kill -0 "$WATCH_PID" 2>/dev/null; then
        kill -TERM "$WATCH_PID" 2>/dev/null || true
        wait "$WATCH_PID" 2>/dev/null || true
    fi
    if [ -n "$SSH_PID" ] && kill -0 "$SSH_PID" 2>/dev/null; then
        kill -TERM "$SSH_PID" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "$SSH_PID" 2>/dev/null || break
            sleep 0.2
        done
        kill -0 "$SSH_PID" 2>/dev/null && kill -KILL "$SSH_PID" 2>/dev/null || true
        wait "$SSH_PID" 2>/dev/null || true
    fi
    rm -f "$ERR_FILE" "$UP_FILE" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

sleep 0.4
rm -f "$ERR_FILE" "$UP_FILE" 2>/dev/null || true
echo "[intercom-tunnel] Connecting via Direct SSH to $REMOTE_HOST..." >&2
t0=$(awk '{print int($1)}' /proc/uptime)
/usr/bin/ssh "${BASE_OPTS[@]}" \
    -o PermitLocalCommand=yes \
    -o LocalCommand="touch $UP_FILE" \
    "$REMOTE_HOST" 2>"$ERR_FILE" &
SSH_PID=$!
(
    SLP_PID=""
    trap '[ -n "$SLP_PID" ] && kill -TERM "$SLP_PID" 2>/dev/null || true; exit 0' TERM INT
    sleep 12 &
    SLP_PID=$!
    wait "$SLP_PID" 2>/dev/null || exit 0
    if [ ! -f "$UP_FILE" ] && kill -0 "$SSH_PID" 2>/dev/null; then
        echo "[intercom-tunnel] Direct SSH handshake/forward stalled >12s; terminating for proxy failover..." >&2
        kill -TERM "$SSH_PID" 2>/dev/null || true
        sleep 1
        kill -0 "$SSH_PID" 2>/dev/null && kill -KILL "$SSH_PID" 2>/dev/null || true
    fi
) &
WATCH_PID=$!
wait "$SSH_PID"
rc=$?
SSH_PID=""
elapsed=$(( $(awk '{print int($1)}' /proc/uptime) - t0 ))
if [ -n "$WATCH_PID" ]; then
    kill -TERM "$WATCH_PID" 2>/dev/null || true
    wait "$WATCH_PID" 2>/dev/null || true
    WATCH_PID=""
fi
was_up=0
[ -f "$UP_FILE" ] && was_up=1
rm -f "$UP_FILE" 2>/dev/null || true
if [ -s "$ERR_FILE" ]; then
    cat "$ERR_FILE" >&2
fi

if [ "$was_up" -eq 1 ] && [ "$elapsed" -ge 15 ]; then
    rm -f "$ERR_FILE" 2>/dev/null || true
    exit "$rc"
fi

# If direct SSH connected and authenticated fine, but the remote server still holds the forwarded port
# from a recently dropped TCP session, proxy fallback will fail with the exact same remote port error.
if [ -s "$ERR_FILE" ] && grep -q "remote port forwarding failed" "$ERR_FILE"; then
    rm -f "$ERR_FILE" 2>/dev/null || true
    echo "[intercom-tunnel] Remote port still held on server (${elapsed}s); waiting for server cleanup..." >&2
    sleep 3
    exit "$rc"
fi
rm -f "$ERR_FILE" 2>/dev/null || true

proxy_was_up=0
if [[ "$SOCKS_PORT" =~ ^[0-9]+$ ]] && ss -tln | grep -Eq ":${SOCKS_PORT}([[:space:]]|\$)"; then
    sleep 1.5
    rm -f "$ERR_FILE" "$UP_FILE" 2>/dev/null || true
    echo "[intercom-tunnel] Direct SSH exited in ${elapsed}s (rc=${rc}), falling back to local proxy ${SOCKS_PROXY}..." >&2
    # Note: OpenSSH prepends "exec " when invoking ProxyCommand via $SHELL -c; never put a leading "exec " here.
    /usr/bin/ssh "${BASE_OPTS[@]}" \
        -o PermitLocalCommand=yes \
        -o LocalCommand="touch $UP_FILE" \
        -o ProxyCommand="nc -w 45 -X 5 -x ${SOCKS_PROXY} %h %p" \
        "$REMOTE_HOST" 2>"$ERR_FILE" &
    SSH_PID=$!
    (
        SLP_PID=""
        trap '[ -n "$SLP_PID" ] && kill -TERM "$SLP_PID" 2>/dev/null || true; exit 0' TERM INT
        sleep 15 &
        SLP_PID=$!
        wait "$SLP_PID" 2>/dev/null || exit 0
        if [ ! -f "$UP_FILE" ] && kill -0 "$SSH_PID" 2>/dev/null; then
            echo "[intercom-tunnel] Proxy SSH handshake/forward stalled >15s; terminating for retry..." >&2
            kill -TERM "$SSH_PID" 2>/dev/null || true
            sleep 1
            kill -0 "$SSH_PID" 2>/dev/null && kill -KILL "$SSH_PID" 2>/dev/null || true
        fi
    ) &
    WATCH_PID=$!
    wait "$SSH_PID"
    rc=$?
    SSH_PID=""
    if [ -n "$WATCH_PID" ]; then
        kill -TERM "$WATCH_PID" 2>/dev/null || true
        wait "$WATCH_PID" 2>/dev/null || true
        WATCH_PID=""
    fi
    [ -f "$UP_FILE" ] && proxy_was_up=1
    if [ -s "$ERR_FILE" ]; then
        cat "$ERR_FILE" >&2
    fi
    rm -f "$ERR_FILE" "$UP_FILE" 2>/dev/null || true
fi

# Prevent rapid restart storms (and sshd MaxStartups / fail2ban blocks) when both direct and proxy fail quickly.
total_elapsed=$(( $(awk '{print int($1)}' /proc/uptime) - t0 ))
if [ "$was_up" -eq 0 ] && [ "$proxy_was_up" -eq 0 ] && [ "$total_elapsed" -lt 12 ]; then
    backoff=$(( 12 - total_elapsed ))
    echo "[intercom-tunnel] Both paths failed in ${total_elapsed}s; backing off ${backoff}s before retry..." >&2
    sleep "$backoff"
fi

exit "$rc"
