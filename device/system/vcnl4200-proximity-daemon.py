#!/usr/bin/env python3
"""
Standalone VCNL4200 Proximity Sensor Daemon
Monitors proximity sensor and triggers wake when display is off and hand approaches
Independent from vp5 - runs as separate systemd service
"""

import json
import math
import subprocess
import time
import sys
import os
import signal
import threading
import logging
from pathlib import Path

# Configuration
I2C_BUS = 0
I2C_ADDR = 0x51
PS_CONF1_REG = 0x03
PS_DATA_REG = 0x08
ALS_CONF_REG = 0x00
ALS_DATA_REG = 0x09
ALS_RESULT = Path("/run/user/1000/thinksmart-als.json")
USER_BRIGHTNESS_PATH = Path("/run/user/1000/user_brightness")
LEGACY_USER_BRIGHTNESS_PATH = Path("/tmp/user_brightness")
HINT_PATH = Path("/run/user/1000/thinksmart-screen.json")
MODE_PATH = Path("/var/lib/thinksmart-monitor/display.json")
_DEFAULT_RUNTIME_DIR = Path("/run/user/1000")
NIGHT_FROM, NIGHT_TO = (0, 0), (6, 30)
NIGHT_LEVEL = 1400
NIGHT_VIEW_PATH = Path("/run/user/1000/thinksmart-night-view")
SUNRISE_PATH = Path("/run/user/1000/thinksmart-sunrise.json")
NIGHT_TEST = False
MIRROR_FLAG_PATH = Path("/run/user/1000/uxplay-mirroring-active")
INTERCOM_FLAG_PATH = Path("/run/user/1000/intercom-call-active")
SLEEP_DIM_BRIGHTNESS = 1600
MAX_BRIGHTNESS = 4095
PROC_ROOT = Path("/proc")
_UID = os.getuid()

# PS_CONF1: PS_IT=4T, PS_DUTY=1/40, PS_PERS=2, PS_SD=0 (active)
PS_CONF1_ENABLED = 0xCA
PS_CONF2_ENABLED = 0x08
PS_CONF1_DISABLED = 0xCB  # PS_SD bit set

# PS_MS register (0x04): high byte bits[2:0] = LED_I
# 000=50mA 001=75mA 010=100mA 011=120mA 100=140mA 101=160mA 110=180mA 111=200mA
PS_MS_REG = 0x04
PS_LED_200MA = 0x0700  # low byte (PS_CONF3) = 0x00, high byte (PS_MS) = 0x07

# Thresholds
PROXIMITY_THRESHOLD = 50   # Counts above this = object detected (ambient ~5-11, hand ~87-1044)
DEBOUNCE_COUNT = 1         # Require N consecutive readings above threshold before waking
WAKE_COOLDOWN = 10.0       # Seconds to wait before triggering wake again
POLLING_INTERVAL = 0.5     # Poll at 2 Hz when blanked; 1 Hz when screen is on
BACKLIGHT_PATH = "/sys/class/backlight/backlight/brightness"

# Blanking value must match idle-blanker.py BLANK_BRIGHTNESS.
# Proximity sensor is enabled when brightness <= this value (screen is blanked).
BLANK_BRIGHTNESS = 10

# Logging
# stdout goes to the journal (persistent, size-capped); a log file of its own grew without bound
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def _num(v) -> bool:
    return type(v) in (int, float) and math.isfinite(v)


def should_wake_night_low(now=None) -> bool:
    if HINT_PATH.parent != _DEFAULT_RUNTIME_DIR and not NIGHT_TEST:
        return False
    ts = time.time() if now is None else now
    try:
        sr = json.loads(SUNRISE_PATH.read_text())
        if isinstance(sr, dict) and "stopped" not in sr:
            st, ramp, hold = sr.get("start"), sr.get("ramp"), sr.get("hold")
            if (_num(st) and _num(ramp) and _num(hold)
                    and 0 < ramp <= 3600 and 0 <= hold <= 7200
                    and st <= ts < st + ramp + hold):
                return False
    except Exception:
        pass
    try:
        nv = json.loads(NIGHT_VIEW_PATH.read_text())
        if isinstance(nv, dict) and _num(nv.get("until")) and ts < nv["until"]:
            return True
    except Exception:
        pass
    lt = time.localtime(ts)
    hm = (lt.tm_hour, lt.tm_min)
    return NIGHT_FROM <= hm < NIGHT_TO if NIGHT_FROM <= NIGHT_TO else (hm >= NIGHT_FROM or hm < NIGHT_TO)


def read_wake_brightness(now=None) -> int:
    if should_wake_night_low(now):
        return NIGHT_LEVEL
    hint, mode = {}, {}
    try:
        h = json.loads(HINT_PATH.read_text())
        if isinstance(h, dict):
            hint = h
    except Exception:
        pass
    try:
        m = json.loads(MODE_PATH.read_text())
        if isinstance(m, dict):
            mode = m
    except Exception:
        pass
    b = hint.get("brightness")
    valid_b = type(b) in (int, float) and math.isfinite(b) and 0 <= b <= 100
    anchor_b = b if valid_b else 100
    anchor = mode.get("anchor")
    is_auto = mode.get("auto") is True and type(anchor) in (int, float) and anchor == anchor_b
    val = None
    if not is_auto and valid_b:
        val = 1400 + round((MAX_BRIGHTNESS - 1400) * max(0, min(100, b)) / 100)
    if val is None:
        for p in (USER_BRIGHTNESS_PATH, LEGACY_USER_BRIGHTNESS_PATH):
            try:
                v = int(p.read_text().strip())
                if BLANK_BRIGHTNESS < v <= MAX_BRIGHTNESS:
                    val = v
                    break
            except Exception:
                pass
    if val is None:
        val = MAX_BRIGHTNESS
    if hint.get("dim"):
        val = min(SLEEP_DIM_BRIGHTNESS, val)
    return val


def resume_webkit_if_idle() -> None:
    if MIRROR_FLAG_PATH.exists() or INTERCOM_FLAG_PATH.exists():
        return
    try:
        entries = os.scandir(PROC_ROOT)
    except Exception:
        return
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                if entry.stat(follow_symlinks=False).st_uid not in (_UID, 1000):
                    continue
                with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                    argv0 = f.read(512).partition(b"\x00")[0]
                if argv0 != b"WebKitWebProcess" and not argv0.endswith(b"/WebKitWebProcess"):
                    continue
                with open(os.path.join(entry.path, "stat"), "r", encoding="utf-8", errors="ignore") as f:
                    state = f.read(256).rsplit(")", 1)[1].split(None, 1)[0]
                if state in ("T", "t"):
                    os.kill(pid, signal.SIGCONT)
            except Exception:
                pass


class ProximitySensor:
    def __init__(self):
        self.running = False
        self.sensor_enabled = False
        self.last_brightness = None
        self._above_count = 0          # consecutive readings above threshold
        self._last_wake_time = None    # time of last wake trigger (for cooldown)
        self.als_enabled = False
        self.last_als = None
        self.bus = None                # smbus2.SMBus, opened on first use
        self.bus_failed = False
        self._stop_event = threading.Event()
        self._stopped = False
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, sig, frame):
        self.running = False
        self._stop_event.set()

    def _bus(self):
        """The I2C bus opened directly (user ubuntu is in the i2c group): reading through
        `sudo i2cget` about once a second filled the journal and auth.log with sudo records.
        None (and sudo below) when /dev/i2c-N isn't ours, e.g. before the group change takes effect."""
        if self.bus is None and not self.bus_failed:
            try:
                from smbus2 import SMBus
                self.bus = SMBus(I2C_BUS)
            except Exception as e:
                self.bus_failed = True
                logger.warning(f"direct I2C unavailable ({e}), using sudo i2cget/i2cset")
        return self.bus

    def _sudo(self, args):
        r = subprocess.run(['sudo', args[0], '-y', str(I2C_BUS), f'0x{I2C_ADDR:02x}', *args[1:]],
                           capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            logger.warning(f"{args[0]} failed (rc={r.returncode}): {r.stderr.strip()}")
        return r

    def i2c_get(self, reg):
        """Read word from i2c register"""
        try:
            bus = self._bus()
            if bus is not None:
                return bus.read_word_data(I2C_ADDR, reg)
            r = self._sudo(['i2cget', f'0x{reg:02x}', 'w'])
            return int(r.stdout.strip(), 16) if r.returncode == 0 else None
        except Exception as e:
            logger.warning(f"i2cget failed: {e}")
            return None

    def i2c_set(self, reg, val_low):
        """Write a single byte to an i2c register."""
        try:
            bus = self._bus()
            if bus is not None:
                bus.write_byte_data(I2C_ADDR, reg, val_low)
                return True
            return self._sudo(['i2cset', f'0x{reg:02x}', f'0x{val_low:02x}', 'b']).returncode == 0
        except Exception as e:
            logger.warning(f"i2cset failed: {e}")
            return False

    def i2c_set_word(self, reg, word_val):
        """Write a 16-bit word to an i2c register (little-endian: low byte first)."""
        try:
            bus = self._bus()
            if bus is not None:
                bus.write_word_data(I2C_ADDR, reg, word_val)
                return True
            return self._sudo(['i2cset', f'0x{reg:02x}', f'0x{word_val:04x}', 'w']).returncode == 0
        except Exception as e:
            logger.warning(f"i2cset word failed: {e}")
            return False

    def poll_als(self, min_interval=1.0):
        """50 ms ALS, 0.024 lx/count; preserve the reserved high byte at 0x01.
        Independent from PS, so proximity wake continues to use its original registers.
        """
        now = time.monotonic()
        if self.last_als is not None and now - self.last_als < min_interval:
            return
        if not self.als_enabled:
            self.als_enabled = self.i2c_set_word(ALS_CONF_REG, 0x0100)
            return  # allow the first integration to finish
        raw = self.i2c_get(ALS_DATA_REG)
        if raw is None:
            return  # don't renew the timestamp of an unsuccessful measurement
        data = {'lux': (raw & 0xffff) * 0.024, 'raw': raw & 0xffff, 'at': time.time()}
        temporary = ALS_RESULT.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps(data))
            if _UID == 0:
                try:
                    os.chown(temporary, 1000, 1000)
                except Exception:
                    pass
            temporary.replace(ALS_RESULT)
            self.last_als = now
        except OSError as e:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning(f"ALS result write failed: {e}")

    def get_brightness(self):
        """Read current backlight brightness"""
        try:
            with open(BACKLIGHT_PATH) as f:
                return int(f.read().strip())
        except Exception as e:
            logger.warning(f"Failed to read brightness: {e}")
            return None

    def enable_sensor(self):
        """Enable proximity sensor at 200mA LED current."""
        if self.sensor_enabled:
            return
        logger.info("Enabling proximity sensor")
        # Set LED current to 200mA (PS_MS reg 0x04, high byte LED_I=7)
        led_ok = self.i2c_set_word(PS_MS_REG, PS_LED_200MA)
        if led_ok and self.i2c_set(PS_CONF1_REG, PS_CONF1_ENABLED):
            logger.info("PS_CONF1 enabled (0xCA), LED=200mA")
            self.sensor_enabled = True
        else:
            logger.warning("Failed to enable sensor")

    def disable_sensor(self):
        """Disable proximity sensor to save power"""
        if not self.sensor_enabled:
            return
        logger.info("Disabling proximity sensor (PS_SD=1)")
        if self.i2c_set(PS_CONF1_REG, PS_CONF1_DISABLED):
            logger.info("Sensor shut down")
            self.sensor_enabled = False
        else:
            logger.warning("Failed to disable sensor")

    def read_proximity(self):
        """Read proximity data"""
        val = self.i2c_get(PS_DATA_REG)
        if val is not None:
            return val & 0xFFFF
        return None

    def trigger_wake(self):
        """Wake the display by restoring backlight brightness directly."""
        now = time.monotonic()
        if self._last_wake_time is not None and now - self._last_wake_time < WAKE_COOLDOWN:
            logger.debug(f"Wake cooldown active ({WAKE_COOLDOWN - (now - self._last_wake_time):.1f}s remaining)")
            return
        logger.warning("=== PROXIMITY DETECTED — WAKING SCREEN ===")
        self._above_count = 0

        resume_webkit_if_idle()
        wake_val = read_wake_brightness()
        woke = False
        try:
            Path(BACKLIGHT_PATH).write_text(f"{wake_val}\n")
            woke = True
            logger.info(f"Screen woken via direct backlight write ({wake_val})")
        except PermissionError:
            try:
                r = subprocess.run(
                    ['sudo', 'tee', BACKLIGHT_PATH],
                    input=f'{wake_val}\n', text=True,
                    capture_output=True, timeout=3
                )
                if r.returncode == 0:
                    woke = True
                    logger.info(f"Screen woken via sudo tee backlight write ({wake_val})")
                else:
                    logger.warning(f"sudo tee backlight failed (rc={r.returncode}): {r.stderr.strip()}")
            except Exception as e:
                logger.warning(f"Backlight write failed: {e}")
        except Exception as e:
            logger.warning(f"Backlight write failed: {e}")
        if woke:
            self._last_wake_time = now

    def run(self):
        """Main daemon loop"""
        logger.info("Proximity daemon started")
        self.running = True

        # Initial state
        brightness = self.get_brightness()
        logger.info(f"Initial brightness: {brightness}")
        self.last_brightness = brightness

        try:
            while self.running:
                try:
                    # Check brightness
                    brightness = self.get_brightness()
                    was_blanked = self.last_brightness is not None and self.last_brightness <= BLANK_BRIGHTNESS
                    is_blanked = (brightness <= BLANK_BRIGHTNESS) if brightness is not None else was_blanked
                    if brightness is not None and was_blanked and not is_blanked:
                        self.last_als = None
                    self.poll_als(10.0 if is_blanked else 1.0)

                    if brightness is not None:
                        if self.last_brightness is None or was_blanked != is_blanked:
                            logger.info(f"Brightness state: {self.last_brightness} -> {brightness} ({'blanked' if is_blanked else 'awake'})")
                        self.last_brightness = brightness

                        # Screen is blanked (brightness at or below BLANK_BRIGHTNESS=10)
                        if is_blanked:
                            if not self.sensor_enabled:
                                self.enable_sensor()
                                self._above_count = 0  # reset debounce on enable

                            # Read proximity
                            prox = self.read_proximity()
                            if prox is not None:
                                logger.debug(f"PS_DATA = 0x{prox:04x} ({prox})")

                                if prox > PROXIMITY_THRESHOLD:
                                    self._above_count += 1
                                    logger.debug(f"Above threshold ({prox} > {PROXIMITY_THRESHOLD}), debounce {self._above_count}/{DEBOUNCE_COUNT}")
                                    if self._above_count >= DEBOUNCE_COUNT:
                                        if self._last_wake_time is None or time.monotonic() - self._last_wake_time >= WAKE_COOLDOWN:
                                            logger.warning(f"PROXIMITY: {prox} counts over {DEBOUNCE_COUNT} readings — triggering wake")
                                        self.trigger_wake()
                                else:
                                    self._above_count = 0

                        # Screen is ON (brightness > BLANK_BRIGHTNESS)
                        else:
                            if self.sensor_enabled:
                                self.disable_sensor()

                    sleep_sec = POLLING_INTERVAL if is_blanked else (POLLING_INTERVAL * 2)
                    self._stop_event.wait(sleep_sec)

                except Exception as e:
                    logger.error(f"Error in loop: {e}")
                    self._stop_event.wait(1)
        finally:
            self.stop()

    def stop(self):
        """Shutdown gracefully"""
        if getattr(self, '_stopped', False):
            return
        self._stopped = True
        self.running = False
        self.disable_sensor()
        if self.als_enabled:
            self.i2c_set_word(ALS_CONF_REG, 0x0101)
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
            self.bus = None
        ALS_RESULT.unlink(missing_ok=True)
        logger.info("Daemon stopped")

if __name__ == '__main__':
    daemon = ProximitySensor()
    daemon.run()
