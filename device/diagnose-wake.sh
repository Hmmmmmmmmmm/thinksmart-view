#!/usr/bin/env bash
# 只读诊断脚本：排查「小智界面能唤醒，但切到主页/音乐/监控等界面无法唤醒」的原因
set -u

FAIL=0
WARN=0

pass() { printf '[\033[32mPASS\033[0m] %s\n' "$1"; }
warn() { printf '[\033[33mWARN\033[0m] %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf '[\033[31mFAIL\033[0m] %s\n' "$1"; FAIL=$((FAIL + 1)); }

echo "=== ThinkSmart View 跨界面语音唤醒诊断 ==="

# 1. 基础语音客户端是否安装与运行
if [ -d /opt/thinksmart/client/current ] && systemctl is-active --quiet thinksmart-client.service; then
  pass "thinksmart-client.service 运行中（后台常驻麦克风与 sherpa-onnx 唤醒）"
else
  fail "未检测到运行中的 thinksmart-client.service（/opt/thinksmart/client/current）。如使用仅靠浏览器录音的网页版小智，切走页面后必然无法唤醒；本仓库需配合 justhalfbit/xiaozhi-thinksmart-client 底包使用。"
fi

# 2. Kiosk 顶层 URL 必须指向小智 :8090
if [ -f /etc/thinksmart-kiosk.conf ]; then
  KIOSK_URL="$(sed -n 's/^URL=\(.*\)/\1/p' /etc/thinksmart-kiosk.conf | tail -n 1 | tr -d '"'\''[:space:]')"
  case "$KIOSK_URL" in
    http://127.0.0.1:8090*|http://localhost:8090*)
      pass "/etc/thinksmart-kiosk.conf URL=$KIOSK_URL（小智作为顶层宿主）"
      ;;
    *)
      fail "/etc/thinksmart-kiosk.conf 当前 URL=$KIOSK_URL，必须保持 http://127.0.0.1:8090/！若设成 :8095 主页，唤醒后不会切回小智界面。修复：sudo kiosk-mode url http://127.0.0.1:8090/ && sudo kiosk-mode restart"
      ;;
  esac
else
  fail "缺少 /etc/thinksmart-kiosk.conf（请参考 device/configs/kiosk/thinksmart-kiosk.conf.example 或运行 sudo bash device/install.sh）"
fi

# 3. Kiosk 注入脚本与叠加层是否存在
if [ -f /usr/local/bin/thinksmart-kiosk.py ] && grep -q 'home_overlay.js' /usr/local/bin/thinksmart-kiosk.py; then
  pass "/usr/local/bin/thinksmart-kiosk.py 已包含 overlay 注入逻辑"
else
  fail "/usr/local/bin/thinksmart-kiosk.py 未更新（仍是旧版），无法向 :8090 注入桌面主页与全屏子页面"
fi

for asset in \
  /usr/local/share/thinksmart-monitor/home_overlay.js \
  /usr/local/share/thinksmart-qqmusic/kiosk_overlay.js \
  /usr/local/share/thinksmart-home/index.html; do
  if [ -f "$asset" ]; then
    pass "存在界面资源: $asset"
  else
    fail "缺少界面资源: $asset"
  fi
done

# 4. WebRTC AEC 与语音工具桥接是否就位
if [ -f /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py ]; then
  pass "存在 AEC 桥接文件 /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py"
else
  fail "缺少 /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py（必须从 device/qqmusic/aec_bridge_sitecustomize.py 重命名为 sitecustomize.py，否则音乐/白噪音外放时没有回声消除会喊不醒）"
fi

if [ -f /etc/systemd/system/thinksmart-client.service.d/aec-bridge.conf ] && grep -q '/usr/local/lib/thinksmart-aec-bridge' /etc/systemd/system/thinksmart-client.service.d/aec-bridge.conf; then
  pass "已配置 thinksmart-client.service.d/aec-bridge.conf"
else
  fail "缺少 /etc/systemd/system/thinksmart-client.service.d/aec-bridge.conf"
fi

if [ -f /run/user/1000/qqmusic-bridge.json ]; then
  if grep -q '"audio_hook": true' /run/user/1000/qqmusic-bridge.json; then
    pass "运行时桥接已激活 (/run/user/1000/qqmusic-bridge.json audio_hook=true)"
  else
    warn "qqmusic-bridge.json 存在但 audio_hook 未激活：$(cat /run/user/1000/qqmusic-bridge.json 2>/dev/null)"
  fi
else
  warn "未找到 /run/user/1000/qqmusic-bridge.json（若刚部署请重启 thinksmart-client.service）"
fi

# 5. 麦克风与 PipeWire / WirePlumber
TARGET_USER="${SUDO_USER:-ubuntu}"
if id "$TARGET_USER" >/dev/null 2>&1 && [ "$TARGET_USER" != "root" ]; then
  U_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
  U_UID="$(id -u "$TARGET_USER")"
  if [ -f "$U_HOME/.config/wireplumber/main.lua.d/51-cd18781y-alsa.lua" ]; then
    pass "用户级 WirePlumber ALSA 配置存在于 $U_HOME/.config/wireplumber/main.lua.d/"
  else
    warn "缺少 $U_HOME/.config/wireplumber/main.lua.d/51-cd18781y-alsa.lua"
  fi
  if su - "$TARGET_USER" -c "XDG_RUNTIME_DIR=/run/user/$U_UID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$U_UID/bus systemctl --user is-active --quiet thinksmart-mic.service" 2>/dev/null; then
    pass "用户态 thinksmart-mic.service 运行中"
  else
    fail "用户态 thinksmart-mic.service 未运行，PipeWire 虚拟麦克风源可能未就绪"
  fi
fi

if [ -f /etc/wireplumber/scripts/suspend-node.lua ]; then
  pass "已安装 /etc/wireplumber/scripts/suspend-node.lua（防 TAS5782M 功放休眠吞提示音）"
else
  warn "缺少 /etc/wireplumber/scripts/suspend-node.lua（唤醒提示音可能因功放冷启动 546ms 而无声）"
fi

echo ""
echo "诊断结果：$FAIL 个错误，$WARN 个警告。"
[ "$FAIL" -eq 0 ]
