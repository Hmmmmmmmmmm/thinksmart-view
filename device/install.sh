#!/usr/bin/env bash
# ThinkSmart View 设备端一键安装与路径修正脚本
# 适用底包：justhalfbit/thinksmart-ubuntu-image (Ubuntu 24.04 ARM64) + xiaozhi-thinksmart-client
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "请使用 sudo 运行：sudo bash device/install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEV_DIR="$SCRIPT_DIR"

TARGET_USER="${SUDO_USER:-ubuntu}"
if ! id "$TARGET_USER" >/dev/null 2>&1 || [ "$TARGET_USER" = "root" ]; then
  if id ubuntu >/dev/null 2>&1; then
    TARGET_USER="ubuntu"
  else
    echo "错误：未找到设备默认用户 ubuntu（或非 root 的 SUDO_USER）。" >&2
    exit 1
  fi
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"

echo "==> 目标仓库目录: $REPO_ROOT"
echo "==> 目标设备用户: $TARGET_USER ($TARGET_HOME)"

# 1. 创建独立 Python 运行环境
if [ ! -x /opt/thinksmart-addons/venv/bin/python ]; then
  echo "==> [1/8] 创建 /opt/thinksmart-addons/venv 并安装 Python 依赖..."
  mkdir -p /opt/thinksmart-addons
  python3 -m venv /opt/thinksmart-addons/venv
  /opt/thinksmart-addons/venv/bin/pip install --upgrade pip >/dev/null
fi
echo "==> [1/8] 校验 Python 运行环境依赖 (aiohttp, numpy, lunar_python)..."
/opt/thinksmart-addons/venv/bin/pip install -q aiohttp numpy lunar_python

# 2. 安装 /usr/local/bin 与 /usr/local/sbin 可执行程序
echo "==> [2/8] 安装可执行程序到 /usr/local/bin 和 /usr/local/sbin..."
BIN_FILES=(
  "$DEV_DIR/home/thinksmart-home.py"
  "$DEV_DIR/qqmusic/thinksmart-qqmusic.py"
  "$DEV_DIR/monitor/thinksmart-monitor.py"
  "$DEV_DIR/monitor/thinksmart_display.py"
  "$DEV_DIR/intercom/thinksmart-intercom.py"
  "$DEV_DIR/intercom/thinksmart-intercom-ui.py"
  "$DEV_DIR/intercom/thinksmart-intercom-tunnel.sh"
  "$DEV_DIR/study/thinksmart-study.py"
  "$DEV_DIR/system/idle-blanker.py"
  "$DEV_DIR/system/screen-wake-daemon.py"
  "$DEV_DIR/system/thinksmart-airplay.py"
  "$DEV_DIR/system/thinksmart-kiosk.py"
  "$DEV_DIR/system/thinksmart-kiosk-session"
  "$DEV_DIR/system/thinksmart-kiosk-app"
  "$DEV_DIR/system/kiosk-mode"
  "$DEV_DIR/system/thinksmart-mic"
  "$DEV_DIR/system/vcnl4200-proximity-daemon.py"
  "$DEV_DIR/system/gpu-watchdog.py"
  "$DEV_DIR/system/usb-automount.sh"
  "$DEV_DIR/system/usb-mode"
)
for f in "${BIN_FILES[@]}"; do
  install -D -m 0755 "$f" "/usr/local/bin/$(basename "$f")"
done

install -D -m 0755 "$DEV_DIR/system/thinksmart-review" /usr/local/sbin/thinksmart-review
install -D -m 0700 "$DEV_DIR/system/thinksmart-set-search-keys" /usr/local/sbin/thinksmart-set-search-keys

# 3. 安装 WebRTC 回声消除 & 语音 MCP 桥接钩子（注意：文件名必须为 sitecustomize.py）
echo "==> [3/8] 安装 thinksmart-client AEC 与语音工具桥接钩子..."
install -D -m 0644 "$DEV_DIR/qqmusic/aec_bridge_sitecustomize.py" /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py
install -D -m 0644 "$DEV_DIR/configs/systemd/system/thinksmart-client.service.d/aec-bridge.conf" \
  /etc/systemd/system/thinksmart-client.service.d/aec-bridge.conf

# 4. 安装静态网页、Kiosk 页面与注入脚本
echo "==> [4/8] 安装静态资源到 /usr/local/share/thinksmart-*..."
install -D -m 0644 "$DEV_DIR/home/index.html" /usr/local/share/thinksmart-home/index.html
install -D -m 0644 "$DEV_DIR/home/apps.json" /usr/local/share/thinksmart-home/apps.json

install -D -m 0644 "$DEV_DIR/qqmusic/index.html" /usr/local/share/thinksmart-qqmusic/index.html
install -D -m 0644 "$DEV_DIR/qqmusic/kiosk_overlay.js" /usr/local/share/thinksmart-qqmusic/kiosk_overlay.js

install -D -m 0644 "$DEV_DIR/monitor/index.html" /usr/local/share/thinksmart-monitor/index.html
install -D -m 0644 "$DEV_DIR/monitor/display.html" /usr/local/share/thinksmart-monitor/display.html
install -D -m 0644 "$DEV_DIR/monitor/home_overlay.js" /usr/local/share/thinksmart-monitor/home_overlay.js
install -D -m 0644 "$DEV_DIR/monitor/servers.example.json" /usr/local/share/thinksmart-monitor/servers.example.json
install -D -m 0644 "$DEV_DIR/monitor/traffic.example.json" /usr/local/share/thinksmart-monitor/traffic.example.json
if [ ! -f /usr/local/share/thinksmart-monitor/servers.json ]; then
  install -m 0644 "$DEV_DIR/monitor/servers.example.json" /usr/local/share/thinksmart-monitor/servers.json
fi
if [ ! -f /usr/local/share/thinksmart-monitor/traffic.json ]; then
  install -m 0644 "$DEV_DIR/monitor/traffic.example.json" /usr/local/share/thinksmart-monitor/traffic.json
fi

install -D -m 0644 "$DEV_DIR/intercom/apple-touch-icon.png" /usr/local/share/thinksmart-intercom/apple-touch-icon.png
install -D -m 0644 "$DEV_DIR/intercom/favicon.svg" /usr/local/share/thinksmart-intercom/favicon.svg

install -D -m 0644 "$DEV_DIR/study/parent.html" /usr/local/share/thinksmart-study/parent.html
install -D -m 0644 "$DEV_DIR/study/apple-touch-icon.png" /usr/local/share/thinksmart-study/apple-touch-icon.png
install -D -m 0644 "$DEV_DIR/study/favicon.svg" /usr/local/share/thinksmart-study/favicon.svg

for f in "$DEV_DIR/kiosk/"*; do
  install -D -m 0644 "$f" "/usr/local/share/thinksmart-kiosk/$(basename "$f")"
done

# 5. 确保 /etc/thinksmart-kiosk.conf 顶层 URL 指向小智宿主页 :8090（绝不能设为 :8095）
echo "==> [5/8] 检查 /etc/thinksmart-kiosk.conf 宿主页配置..."
if [ ! -f /etc/thinksmart-kiosk.conf ]; then
  install -D -m 0644 "$DEV_DIR/configs/kiosk/thinksmart-kiosk.conf.example" /etc/thinksmart-kiosk.conf
else
  CUR_URL="$(sed -n 's/^URL=\(.*\)/\1/p' /etc/thinksmart-kiosk.conf | tail -n 1 | tr -d '"'\''[:space:]')"
  case "$CUR_URL" in
    http://127.0.0.1:8095*|http://localhost:8095*)
      echo "    检测到 URL 指向了主页 :8095（会导致无法唤醒小智），正在自动改回 http://127.0.0.1:8090/ ..."
      /usr/local/bin/kiosk-mode url http://127.0.0.1:8090/
      ;;
    "")
      /usr/local/bin/kiosk-mode url http://127.0.0.1:8090/
      ;;
  esac
fi

# 6. 安装系统级 systemd / udev / sysctl / WirePlumber 配置
echo "==> [6/8] 安装系统级 systemd、udev、sysctl 与 WirePlumber 配置..."
for f in "$DEV_DIR/configs/systemd/system/"*.service; do
  install -D -m 0644 "$f" "/etc/systemd/system/$(basename "$f")"
done
install -D -m 0644 "$DEV_DIR/configs/systemd/system/thinksmart-kiosk.service.d/override.conf" \
  /etc/systemd/system/thinksmart-kiosk.service.d/override.conf

for f in "$DEV_DIR/configs/udev/"*.rules; do
  install -D -m 0644 "$f" "/etc/udev/rules.d/$(basename "$f")"
done
for f in "$DEV_DIR/configs/sysctl/"*.conf; do
  install -D -m 0644 "$f" "/etc/sysctl.d/$(basename "$f")"
done

install -D -m 0644 "$DEV_DIR/configs/wireplumber-system/scripts/suspend-node.lua" \
  /etc/wireplumber/scripts/suspend-node.lua
install -D -m 0644 "$DEV_DIR/configs/wireplumber-system/bluetooth.lua.d/60-thinksmart-speaker.lua" \
  /etc/wireplumber/bluetooth.lua.d/60-thinksmart-speaker.lua

# 7. 安装用户级 PipeWire / WirePlumber / systemd-user 配置到 ~/.config/（并安装 xiaozhi-hub overrides 入口）
echo "==> [7/8] 安装用户级 PipeWire / WirePlumber / systemd 配置到 $TARGET_HOME/.config/..."
for sub in client.conf.d pipewire-pulse.conf.d pipewire.conf.d; do
  mkdir -p "$TARGET_HOME/.config/pipewire/$sub"
  for f in "$DEV_DIR/configs/pipewire/$sub/"*.conf; do
    install -m 0644 "$f" "$TARGET_HOME/.config/pipewire/$sub/$(basename "$f")"
  done
done

for sub in bluetooth.lua.d main.lua.d; do
  mkdir -p "$TARGET_HOME/.config/wireplumber/$sub"
  for f in "$DEV_DIR/configs/wireplumber/$sub/"*.lua; do
    install -m 0644 "$f" "$TARGET_HOME/.config/wireplumber/$sub/$(basename "$f")"
  done
done

mkdir -p "$TARGET_HOME/.config/systemd/user/xiaozhi-hub.service.d"
for f in "$DEV_DIR/configs/systemd/user/"*.service; do
  install -m 0644 "$f" "$TARGET_HOME/.config/systemd/user/$(basename "$f")"
done
# 仅安装正式 drop-in，刻意跳过 proxy.conf.example，避免无本地代理时阻断 xiaozhi-hub
install -m 0644 "$DEV_DIR/configs/systemd/user/xiaozhi-hub.service.d/thinksmart-overrides.conf" \
  "$TARGET_HOME/.config/systemd/user/xiaozhi-hub.service.d/thinksmart-overrides.conf"

chown -R "$TARGET_UID:$TARGET_GID" \
  "$TARGET_HOME/.config/pipewire" \
  "$TARGET_HOME/.config/wireplumber" \
  "$TARGET_HOME/.config/systemd"

install -D -m 0644 "$DEV_DIR/xiaozhi-overrides/sitecustomize.py" \
  /opt/thinksmart-addons/xiaozhi-hub-overrides/sitecustomize.py

# 8. 重载规则与服务
echo "==> [8/8] 重载 udev、sysctl 与 systemd 服务..."
udevadm control --reload-rules >/dev/null 2>&1 || true
sysctl --system >/dev/null 2>&1 || true
systemctl daemon-reload

SYSTEM_UNITS=(
  display-guard.service
  screen-wake.service
  thinksmart-gpu-watchdog.service
  thinksmart-home.service
  thinksmart-qqmusic.service
  thinksmart-monitor.service
  thinksmart-study.service
  thinksmart-intercom.service
  thinksmart-kiosk.service
)
for u in "${SYSTEM_UNITS[@]}"; do
  systemctl enable "$u" >/dev/null 2>&1 || true
  systemctl restart "$u" >/dev/null 2>&1 || true
done

if systemctl list-unit-files thinksmart-client.service >/dev/null 2>&1; then
  systemctl restart thinksmart-client.service >/dev/null 2>&1 || true
fi

USER_ENV="XDG_RUNTIME_DIR=/run/user/$TARGET_UID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$TARGET_UID/bus"
if su - "$TARGET_USER" -c "$USER_ENV systemctl --user status" >/dev/null 2>&1; then
  su - "$TARGET_USER" -c "$USER_ENV systemctl --user daemon-reload" || true
  for u in thinksmart-mic.service tas5782m-preinit.service idle-blanker.service thinksmart-airplay.service; do
    su - "$TARGET_USER" -c "$USER_ENV systemctl --user enable '$u'" >/dev/null 2>&1 || true
  done
  su - "$TARGET_USER" -c "$USER_ENV systemctl --user restart pipewire pipewire-pulse wireplumber thinksmart-mic.service idle-blanker.service thinksmart-airplay.service" >/dev/null 2>&1 || true
  su - "$TARGET_USER" -c "$USER_ENV systemctl --user start tas5782m-preinit.service" >/dev/null 2>&1 || true
else
  echo "提示：用户态 systemd 会话当前未运行，请重启设备后自动拉起用户态服务。"
fi

echo ""
echo "安装完成！建议运行以下命令自检跨界面唤醒链路："
echo "  bash $DEV_DIR/diagnose-wake.sh"
