#!/usr/bin/env python3
"""Standalone QQ Music Touchscreen & Voice Service for Lenovo ThinkSmart View.

Completely independent of /opt/thinksmart/client/ so XiaoZhi OTA updates (thinksmart-update)
never overwrite or conflict with this service.

Features:
- Serves the 1280x800 Touchscreen QQ Music Car/Pad UI on http://0.0.0.0:8091/
- QQ Music Official Search, Toplists (热歌榜/飙升榜/新歌榜/流行指数榜/港台/内地/欧美),
  HD Album Art, and LRC Synchronized Lyrics
- Direct on-device QQ QR Code Login (手机QQ / QQ音乐App 扫码登录) + QQ号歌单一键同步
- USB Flash Drive (/mnt/usb) & Local Music support
- 48 kHz float32 stereo decoding fed through the non-invasive AEC Unix socket bridge
  (/run/user/1000/qqmusic-aec.sock) so "小麦小麦" wake word echo cancellation works
  seamlessly while music is playing (with automatic PipeWire pacat fallback)
- Watches XiaoZhi's SSE stream (http://127.0.0.1:8090/events) externally to auto-duck
  music during voice conversations and auto-end the 8s follow-up window when starting music.
"""
import asyncio
import base64
import concurrent.futures
import contextlib
import html
import http.cookiejar
import ipaddress
import json
import logging
import math
import os
import queue
import random
import re
import secrets
import signal
import socket
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aiohttp import web

try:  # round 109: sleep-timer fade-out works on the PCM blocks; numpy when available
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None
from array import array as _array

logging.basicConfig(level=logging.INFO, format='%(levelname)s qqmusic: %(message)s')
log = logging.getLogger('qqmusic')

OUT_RATE = 48000
CONFIG_PATH = Path('/home/ubuntu/.config/thinksmart-qqmusic/config.json')
SESSION_PATH = Path('/home/ubuntu/.config/thinksmart-qqmusic/session.json')
AMBIENT_DIR = Path('/home/ubuntu/.config/thinksmart-qqmusic/ambient')
UI_HTML_PATH = Path('/usr/local/share/thinksmart-qqmusic/index.html')
AEC_SOCK_PATH = '/run/user/1000/qqmusic-aec.sock'

AMBIENT_PRESETS = {
    'rain': {'id': 'rain', 'name': '窗外细雨', 'icon': '🌧️', 'desc': '真实屋檐落雨与林叶雨声 · 沉浸读写', 'mid': '002hY0WV09MJBV'},
    'waves': {'id': 'waves', 'name': '海岸慢浪', 'icon': '🌊', 'desc': '真实沙滩慢浪轻拍与退潮 · 舒压助眠', 'mid': '002LY8IS2yJwjf'},
    'forest': {'id': 'forest', 'name': '林间微风', 'icon': '🌲', 'desc': '真实山林微风拂叶与远鸟 · 清醒专注', 'mid': '00433gdV1PqMvf'},
    'stream': {'id': 'stream', 'name': '山涧溪流', 'icon': '💧', 'desc': '真实3D山涧清泉潺潺流水 · 屏蔽杂音', 'mid': '001r6u9J29OLyo'},
    'night': {'id': 'night', 'name': '夏夜虫鸣', 'icon': '🌙', 'desc': '真实静夜微风与轻柔虫鸣 · 睡前安神', 'mid': '000Nm2Gk2CQ2Fy'},
    'sleep_piano': {'id': 'sleep_piano', 'name': '雨夜轻琴', 'icon': '🎹', 'desc': '细雨伴温柔舒缓轻钢琴 · 伴读入睡', 'mid': '000KgHAM0GrU8C'},
    'fireplace': {'id': 'fireplace', 'name': '壁炉柴火', 'icon': '🔥', 'desc': '温暖松木噼啪轻燃声 · 围炉夜读', 'mid': '001QsCxP0bXlma'},
    'library': {'id': 'library', 'name': '书房自习', 'icon': '📖', 'desc': '安静书房轻翻书页与落笔 · 沉浸伴学', 'mid': '001f9TaR0ZwJJF'},
    'singing_bowl': {'id': 'singing_bowl', 'name': '禅意颂钵', 'icon': '🎐', 'desc': '空灵颂钵泛音自然余韵 · 冥想安神', 'mid': '001WQ8rf2c4Ord'},
    # 2026-11-12 (Claude): generated on the device (exactly periodic, so the loop has no seam)
    'white_noise': {'id': 'white_noise', 'name': '柔和白噪音', 'icon': '🌫️', 'desc': '均匀柔和的白噪音 · 盖住杂音', 'mid': ''},
    'pink_noise': {'id': 'pink_noise', 'name': '粉红噪音', 'icon': '🌸', 'desc': '比白噪音更柔和 · 安稳入睡', 'mid': ''},
    'brown_noise': {'id': 'brown_noise', 'name': '低沉褐噪音', 'icon': '🌰', 'desc': '像远处瀑布的低沉声 · 深度放松', 'mid': ''},
}

AMBIENT_RECIPES = {
    'deep_study': {'id': 'deep_study', 'name': '深度自习', 'icon': '📚', 'preset': 'rain', 'layer': 'library', 'layer_gain': 0.45, 'desc': '窗外细雨 + 书房自习 45%'},
    'cozy_read': {'id': 'cozy_read', 'name': '围炉夜读', 'icon': '🛋️', 'preset': 'fireplace', 'layer': 'sleep_piano', 'layer_gain': 0.45, 'desc': '壁炉柴火 + 雨夜轻琴 45%'},
    'deep_sleep': {'id': 'deep_sleep', 'name': '深度入眠', 'icon': '🌙', 'preset': 'waves', 'layer': 'singing_bowl', 'layer_gain': 0.35, 'desc': '海岸慢浪 + 禅意颂钵 35%'},
}

# ---- 2026-11-12 (Claude): sleep sounds and sleep sessions ----------------------------------------------------------
# The loops are FLAC: ffmpeg -stream_loop repeats FLAC sample-exactly, the old 60 s AAC loops left a ~19 ms gap every
# lap. Each is a 2-5 minute piece of the full QQ track with a crossfaded seam (the piano piece whole), all at -20 LUFS.
AMBIENT_EXTS = ('.flac', '.m4a')
SLEEP_STATE_PATH = Path('/home/ubuntu/.config/thinksmart-qqmusic/sleep.json')
BACKLIGHT_PATH = Path('/sys/class/backlight/backlight/brightness')
SLEEP_BLANK = 10                # the idle blanker counts a backlight at or below 10 as a blanked screen
SLEEP_MORNING = (6, 30)         # Product spec 11-12: the night is 00:00-06:30 every day. All-night sessions end here, and the
SLEEP_DAY_END = (0, 0)          # day volume comes back here for a session that ended in the night (else right away)
SLEEP_EVENING = (20, 0)         # from here to the morning the night view may turn the screen off without a session
NIGHT_VIEW_PATH = Path('/run/user/1000/thinksmart-night-view')   # the night view is up: the blanker keeps it dim
SUNRISE_PATH = Path('/run/user/1000/thinksmart-sunrise.json')    # 温柔叫醒: the blanker brightens the screen slowly
CALL_FLAG_PATH = Path('/run/user/1000/intercom-call-active')
MIRROR_FLAG_PATH = Path('/run/user/1000/uxplay-mirroring-active')
SLEEP_WAKE_SOUND = 'forest'     # 林间微风: wind in the leaves, birds far away
SLEEP_WAKE_TIME = (6, 30)       # 温柔叫醒: the default time; the user sets it on the sleep page (04:30-10:00)
SLEEP_WAKE_VOLUME = (20, 50)    # Product spec: the volume goes from 20% up to 50% over SLEEP_WAKE_RAMP ...
SLEEP_WAKE_RAMP = 600           # ... 10 minutes ...
SLEEP_WAKE_HOLD = 300           # ... stays at 50% for 5 more ...
SLEEP_WAKE_FADE = 60            # ... and fades out over a minute (2 s after a touch)
SLEEP_WAKE_SCREEN_MAX = 50      # Product spec: the screen brightens from its own level up to 50% (the brightness panel's %)
WEEKDAY_NAMES = '一二三四五六日'
try:   # the calendar's holidays (the home page uses the same library): no 温柔叫醒 on a day off
    import sys as _sys
    if '/opt/thinksmart-addons/vendor' not in _sys.path:
        _sys.path.append('/opt/thinksmart-addons/vendor')
    from lunar_python.util import HolidayUtil as _HolidayUtil
except Exception:   # noqa: BLE001 - without it, only weekends count as days off
    _HolidayUtil = None
SLEEP_DEFAULT_VOLUME = 25
SLEEP_DEFAULT_MINUTES = 45
SLEEP_DEFAULT_FADE = 600        # a sleep session fades out over its last 10 minutes
SLEEP_NIGHT_SETTLE = 1800       # all night: after 30 minutes the sound goes down ...
SLEEP_NIGHT_RAMP = 600          # ... over 10 minutes ...
SLEEP_NIGHT_DB = -10.0          # ... by this much, until the morning
SLEEP_PLAYLISTS = (             # checked 11-12: most of their songs play with this account (paid-only songs are skipped)
    {'id': '8401506970', 'name': '助眠钢琴', 'desc': '经典助眠钢琴曲 · 安静沉入梦'},
    {'id': '7275679373', 'name': '睡前轻音乐', 'desc': '舒缓纯音乐 · 放松入睡'},
    {'id': '7333764360', 'name': '钢琴摇篮曲', 'desc': '森林童谣钢琴版 · 哄孩子睡'},
)
SLEEP_COMBOS = (               # 11-12 (Product spec): every sleep sound's matching second layer: main, layer, share, name
    ('rain', 'fireplace', 0.40, '雨夜壁炉'),
    ('waves', 'singing_bowl', 0.35, '深度入眠'),
    ('forest', 'stream', 0.35, '林间溪涧'),
    ('stream', 'sleep_piano', 0.30, '溪边琴音'),
    ('night', 'waves', 0.35, '海边夏夜'),
    ('fireplace', 'rain', 0.45, '炉边听雨'),
    ('white_noise', 'rain', 0.30, '白噪细雨'),
    ('pink_noise', 'waves', 0.35, '粉噪海浪'),
    ('brown_noise', 'rain', 0.40, '褐噪细雨'),
    ('singing_bowl', 'stream', 0.35, '溪畔冥想'),
    ('sleep_piano', 'fireplace', 0.30, '围炉轻琴'),
)  # the home page has the same list (SP_PAIR)
SLEEP_SOUND_NAMES = {           # what people call them (voice / last choice) -> ambient preset or recipe
    '雨声': 'rain', '下雨': 'rain', '下雨声': 'rain', '细雨': 'rain', '窗外细雨': 'rain',
    '海浪': 'waves', '海浪声': 'waves', '大海': 'waves', '海岸慢浪': 'waves', '潮汐': 'waves',
    '森林': 'forest', '微风': 'forest', '风声': 'forest', '林间微风': 'forest', '鸟鸣': 'forest',
    '溪流': 'stream', '流水': 'stream', '溪水': 'stream', '山涧溪流': 'stream', '流水声': 'stream',
    '虫鸣': 'night', '夏夜虫鸣': 'night', '虫鸣声': 'night',
    '轻琴': 'sleep_piano', '雨夜轻琴': 'sleep_piano',
    '壁炉': 'fireplace', '柴火': 'fireplace', '壁炉柴火': 'fireplace', '篝火': 'fireplace',
    '书房': 'library', '翻书': 'library', '书房自习': 'library',
    '颂钵': 'singing_bowl', '禅意颂钵': 'singing_bowl', '冥想': 'singing_bowl',
    '白噪音': 'white_noise', '柔和白噪音': 'white_noise', '粉红噪音': 'pink_noise', '粉噪音': 'pink_noise',
    '褐噪音': 'brown_noise', '褐噪': 'brown_noise', '棕噪音': 'brown_noise', '低沉褐噪音': 'brown_noise',
    '深度入眠': 'deep_sleep', '深度助眠': 'deep_sleep', '围炉夜读': 'cozy_read', '深度自习': 'deep_study',
}


def _next_sleep_restore_at(now=None) -> float:
    """When the day volume comes back: in a few seconds in the day, at the next 06:30 at night (00:00-06:30)."""
    now = time.time() if now is None else now
    if not _is_sleep_night(now):
        return now + 3.0
    return now + (_seconds_until('%02d:%02d' % SLEEP_MORNING, now) or 0.0)


def _seconds_until(hhmm, now=None):
    """Seconds from now to the next HH:MM (today, or tomorrow when it is less than a minute away); None if invalid."""
    m = re.match(r'^\s*(\d{1,2})\s*[:：点]\s*(\d{1,2})?\s*(?:分)?\s*$', str(hhmm or ''))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    now = time.time() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
    if target - now < 60:
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, h, mi, 0, 0, 0, -1))
    return target - now


def _in_hm_window(hm, start, end) -> bool:
    return start <= hm < end if start <= end else (hm >= start or hm < end)


def _is_sleep_night(now=None) -> bool:
    lt = time.localtime(time.time() if now is None else now)
    return _in_hm_window((lt.tm_hour, lt.tm_min), SLEEP_DAY_END, SLEEP_MORNING)


def _is_sleep_evening(now=None) -> bool:
    lt = time.localtime(time.time() if now is None else now)
    return _in_hm_window((lt.tm_hour, lt.tm_min), SLEEP_EVENING, SLEEP_MORNING)


def _wake_day_info(y, m, d):
    """(day off?, why) by the calendar: weekends and holidays are off, a 调休 workday is a workday."""
    import datetime as _dt
    wd = _dt.date(y, m, d).isoweekday()
    if _HolidayUtil is not None:
        try:
            h = _HolidayUtil.getHoliday(y, m, d)
        except Exception:   # noqa: BLE001
            h = None
        if h is not None:
            return (not h.isWork()), (f'{h.getName()}调休上班' if h.isWork() else f'{h.getName()}假期')
    return wd >= 6, ('周末' if wd >= 6 else '')


def _wake_hm(st) -> tuple:
    m = re.match(r'^(\d{1,2}):(\d{2})$', str((st or {}).get('wake_time') or ''))
    if m and (4, 30) <= (int(m.group(1)), int(m.group(2))) <= (10, 0) and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2))
    return SLEEP_WAKE_TIME


def _wake_next(st, now=None) -> dict:
    """The next 温柔叫醒 morning: today before its time, else tomorrow; whether it is a day off."""
    now = time.time() if now is None else now
    h, mi = _wake_hm(st)
    lt = time.localtime(now)
    at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
    later = now >= at
    if later:
        lt = time.localtime(at + 86400 + 3600)   # tomorrow (an hour of slack for clock changes)
    off, why = _wake_day_info(lt.tm_year, lt.tm_mon, lt.tm_mday)
    return {'date': '%04d-%02d-%02d' % (lt.tm_year, lt.tm_mon, lt.tm_mday), 'when': '明天' if later else '今天',
            'weekday': '周' + WEEKDAY_NAMES[lt.tm_wday], 'off': off, 'why': why, 'time': '%d:%02d' % (h, mi)}


def _sunrise_mark_stopped():
    """Tell the screen owner the wake-up is over (it goes back to its normal brightness)."""
    try:
        data = json.loads(SUNRISE_PATH.read_text(encoding='utf-8'))
        if isinstance(data, dict) and 'stopped' not in data:
            data['stopped'] = int(time.time())
            _write_runtime_json(SUNRISE_PATH, data)
    except (OSError, ValueError):
        pass


def _write_runtime_json(path: Path, data: dict):
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(data), encoding='utf-8')
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _read_sleep_state() -> dict:
    try:
        d = json.loads(_read_regular_text(SLEEP_STATE_PATH, 65536, private=True))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_sleep_state(d: dict):
    try:
        SLEEP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SLEEP_STATE_PATH.with_name('.sleep.json.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, SLEEP_STATE_PATH)
    except OSError as e:
        log.warning('sleep state not saved: %s', e)


def _clean_sleep_sound(raw):
    """{'kind': 'ambient', 'preset', 'layer', 'layer_gain'} | {'kind': 'playlist', 'id'} | {'kind': 'current'} | None."""
    if isinstance(raw, str):
        raw = {'name': raw}
    if not isinstance(raw, dict):
        return None
    kind = raw.get('kind')
    if kind == 'current':
        return {'kind': 'current'}
    if kind == 'playlist' or (kind is None and raw.get('id')):
        pid = str(raw.get('id') or '')
        return {'kind': 'playlist', 'id': pid} if any(p['id'] == pid for p in SLEEP_PLAYLISTS) else None
    name = str(raw.get('preset') or raw.get('name') or '').strip().lower()
    for suf in ('的声音', '声音', '音乐', '白噪音模式'):
        if name.endswith(suf) and name[:-len(suf)] in SLEEP_SOUND_NAMES:
            name = name[:-len(suf)]
    for m, l, g, n in SLEEP_COMBOS:
        if name == n:
            return {'kind': 'ambient', 'preset': m, 'layer': l, 'layer_gain': g}
    for p in SLEEP_PLAYLISTS:
        if name and (name == p['name'] or name == p['id'] or (len(name) >= 2 and name in p['name'])):
            return {'kind': 'playlist', 'id': p['id']}
    if name in ('钢琴', '助眠音乐', '睡眠音乐', '催眠曲', '轻音乐', '纯音乐'):
        return {'kind': 'playlist', 'id': SLEEP_PLAYLISTS[0]['id'] if name in ('钢琴', '助眠音乐', '睡眠音乐', '催眠曲') else SLEEP_PLAYLISTS[1]['id']}
    if name in ('摇篮曲', '儿歌', '宝宝'):
        return {'kind': 'playlist', 'id': SLEEP_PLAYLISTS[2]['id']}
    preset = SLEEP_SOUND_NAMES.get(name, name)
    if preset in AMBIENT_RECIPES:
        return {'kind': 'ambient', 'preset': preset, 'layer': '', 'layer_gain': None}
    if preset not in AMBIENT_PRESETS:
        return None
    lay = str(raw.get('layer') or '').strip().lower()
    lay = SLEEP_SOUND_NAMES.get(lay, lay)
    lay = lay if (lay in AMBIENT_PRESETS and lay != preset) else ''
    lg = raw.get('layer_gain')
    lg = round(max(0.1, min(1.0, float(lg))), 2) if (lay and type(lg) in (int, float) and math.isfinite(lg)) else None
    return {'kind': 'ambient', 'preset': preset, 'layer': lay, 'layer_gain': lg}


def _sleep_sound_label(snd) -> str:
    if not snd:
        return ''
    if snd['kind'] == 'playlist':
        return next((p['name'] for p in SLEEP_PLAYLISTS if p['id'] == snd['id']), '助眠音乐')
    if snd['kind'] == 'current':
        return '正在播放的'
    for m, l, _g, n in SLEEP_COMBOS:
        if snd.get('preset') == m and snd.get('layer') == l:
            return n
    p = snd.get('preset')
    if p in AMBIENT_RECIPES:
        return AMBIENT_RECIPES[p]['name']
    name = AMBIENT_PRESETS.get(p, {}).get('name', '')
    lay = snd.get('layer')
    return f'{name} + {AMBIENT_PRESETS[lay]["name"]}' if (lay and lay in AMBIENT_PRESETS) else name


def _qq_playable_tracks(tracks: list) -> list:
    """Songs this account can play (one batched vkey request per 60 songs); on any error the list is kept as is."""
    out = []
    try:
        cfg = load_config()
        for i in range(0, min(len(tracks), 120), 60):
            part = [t for t in tracks[i:i + 60] if t.get('mid')]
            payload = {'comm': _qq_comm(), 'req_0': {'module': 'vkey.GetVkeyServer', 'method': 'CgiGetVkey', 'param': {
                'guid': '10000', 'songmid': [t['mid'] for t in part],
                'filename': [f"C400{t.get('media_mid') or t['mid']}.m4a" for t in part],
                'songtype': [0] * len(part), 'uin': str(cfg.get('uin') or '10000'), 'loginflag': 1, 'platform': '20'}}}
            req = urllib.request.Request('https://u.y.qq.com/cgi-bin/musicu.fcg', data=json.dumps(payload).encode('utf-8'),
                                         headers=_qq_headers())
            with _safe_urlopen(req, timeout=6) as r:
                d = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
            ok = {x.get('songmid') for x in (d.get('req_0', {}).get('data', {}).get('midurlinfo') or []) if x.get('purl')}
            out.extend(t for t in part if t['mid'] in ok)
    except Exception as e:
        log.warning('sleep playlist check failed (%s); playing it unchecked', type(e).__name__)
        return tracks
    return out

LOCAL_DIRS = [
    Path('/mnt/usb'),
    Path('/home/ubuntu/Music'),
    Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/music'),
]
AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.wma'}

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

TOPLISTS = [
    {'id': 26, 'name': '🔥 热歌榜', 'desc': 'QQ音乐全网最热流行单曲'},
    {'id': 62, 'name': '🚀 飙升榜', 'desc': '热度急速上升的热门新曲'},
    {'id': 27, 'name': '🆕 新歌榜', 'desc': '最新发行的华语及全球新歌'},
    {'id': 4,  'name': '💎 流行指数榜', 'desc': '潮流风向标热门金曲'},
    {'id': 5,  'name': '🇨🇳 内地榜', 'desc': '内地最受欢迎流行歌曲'},
    {'id': 59, 'name': '🇭🇰 香港地区榜', 'desc': '经典与最新粤语金曲'},
    {'id': 61, 'name': '🇹🇼 台湾地区榜', 'desc': '华语流行经典热歌'},
    {'id': 3,  'name': '🌍 欧美榜', 'desc': '欧美流行热单精选'},
    {'id': 16, 'name': '🇰🇷 韩国榜', 'desc': 'K-Pop 潮流韩语热歌榜'},
    {'id': 17, 'name': '🇯🇵 日本榜', 'desc': 'J-Pop 与动漫流行金曲'},
]

_SESSION_LOCK = threading.Lock()
_CFG_LOCK = threading.RLock()
_CFG_CACHE = None
_CFG_SIG = None
_REFRESH_LOCK = threading.Lock()
_LAST_REFRESH_FAIL = (None, 0.0)
_QR_LOCK = threading.Lock()
_QR_SESSIONS = {}
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_RING_VOL_PATH = Path('/home/ubuntu/.local/share/thinksmart-client/alarms/ring.json')
_MAX_HTTP_BYTES = 2 * 1024 * 1024


def _read_regular_text(path: Path, max_bytes: int = 2 * 1024 * 1024, private: bool = False) -> str:
    if private:
        with contextlib.suppress(OSError):
            pst = os.lstat(path.parent)
            if stat.S_ISDIR(pst.st_mode) and (pst.st_mode & 0o077) != 0 and pst.st_uid == os.getuid():
                os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError('Not a regular bounded file')
        if private and (st.st_mode & 0o077) != 0 and st.st_uid == os.getuid():
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
        chunks = []
        rem = max_bytes + 1
        while rem > 0:
            b = os.read(fd, min(65536, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise OSError('File exceeds max_bytes')
        return raw.decode('utf-8')
    finally:
        os.close(fd)


def _read_http_bytes(resp, max_bytes: int = _MAX_HTTP_BYTES) -> bytes:
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('HTTP response exceeds maximum size')
    return data


_BLOCKED_HOST_SUFFIXES = (
    '.localhost', '.local', '.internal', '.lan', '.home.arpa',
    '.nip.io', '.sslip.io', '.xip.io', '.localtest.me', '.lvh.me',
)


def _is_safe_stream_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not u or len(u) > 4096 or any(ord(c) <= 0x20 or ord(c) == 0x7f for c in u):
        return False
    try:
        sp = urllib.parse.urlsplit(u)
        _ = sp.port
    except ValueError:
        return False
    if sp.scheme not in ('http', 'https') or sp.username is not None or sp.password is not None:
        return False
    host = (sp.hostname or '').strip().lower().rstrip('.')
    if not host or host in ('localhost', 'ip6-localhost', 'ip6-loopback') or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return '.' in host
    if getattr(ip, 'ipv4_mapped', None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def _has_only_global_ips(host: str) -> bool:
    h = (host or '').strip().lower().rstrip('.')
    if not h or h in ('localhost', 'ip6-localhost', 'ip6-loopback') or h.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(h)
        if getattr(ip, 'ipv4_mapped', None) is not None:
            ip = ip.ipv4_mapped
        return ip.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = (info[4][0] or '').split('%', 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if getattr(ip, 'ipv4_mapped', None) is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    return True


def _cookie_root(hn: str) -> str:
    h = (hn or '').strip().lower().rstrip('.')
    for root in ('qq.com', 'gtimg.cn', 'bilibili.com'):
        if h == root or h.endswith('.' + root):
            return root
    return h


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        abs_url = urllib.parse.urljoin(req.full_url, newurl)
        if not _is_safe_stream_url(abs_url):
            if fp is not None:
                with contextlib.suppress(Exception):
                    fp.close()
            raise urllib.error.URLError('Blocked unsafe redirect URL')
        old_sp = urllib.parse.urlsplit(req.full_url)
        new_sp = urllib.parse.urlsplit(abs_url)
        if old_sp.scheme == 'https' and new_sp.scheme != 'https':
            if fp is not None:
                with contextlib.suppress(Exception):
                    fp.close()
            raise urllib.error.URLError('Blocked HTTPS to HTTP redirect downgrade')
        if not _has_only_global_ips(new_sp.hostname or ''):
            if fp is not None:
                with contextlib.suppress(Exception):
                    fp.close()
            raise urllib.error.URLError('Blocked redirect to non-global IP')
        if fp is not None:
            body = fp.read(65537)
            if len(body) > 65536:
                with contextlib.suppress(Exception):
                    fp.close()
                raise urllib.error.URLError('Redirect response body too large')
        new_req = super().redirect_request(req, fp, code, msg, headers, abs_url)
        if new_req is not None and _cookie_root(old_sp.hostname) != _cookie_root(new_sp.hostname):
            for hdr_d in (new_req.headers, new_req.unredirected_hdrs):
                for hk in [k for k in list(hdr_d.keys()) if k.lower() in ('cookie', 'authorization')]:
                    hdr_d.pop(hk, None)
        return new_req


_ORIG_URLOPEN = urllib.request.urlopen
_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler)


def _safe_urlopen(req, timeout: float = 6):
    if urllib.request.urlopen is not _ORIG_URLOPEN:
        return urllib.request.urlopen(req, timeout=timeout)
    return _SAFE_OPENER.open(req, timeout=timeout)


_SAFE_FFMPEG_URL_CACHE = {}
_SAFE_FFMPEG_URL_LOCK = threading.Lock()


def _resolve_safe_ffmpeg_url(url: str, hdrs: dict = None) -> str:
    cur = (url or '').strip()
    if not _is_safe_stream_url(cur):
        return ''
    sp0 = urllib.parse.urlsplit(cur)
    if not _has_only_global_ips(sp0.hostname or ''):
        return ''
    if urllib.request.urlopen is not _ORIG_URLOPEN:
        return cur

    cur_hdrs = {
        str(k).translate({10: None, 13: None}): str(v).translate({10: None, 13: None})
        for k, v in (hdrs or {}).items()
        if str(k).translate({10: None, 13: None})
    }
    cur_hdrs.setdefault('User-Agent', UA)
    cache_key = (cur, tuple(sorted((k.lower(), v) for k, v in cur_hdrs.items())))
    now_m = time.monotonic()
    with _SAFE_FFMPEG_URL_LOCK:
        hit = _SAFE_FFMPEG_URL_CACHE.get(cache_key)
    if hit and (now_m - hit[0]) < 600.0:
        hit_host = urllib.parse.urlsplit(hit[1]).hostname or ''
        if _is_safe_stream_url(hit[1]) and _has_only_global_ips(hit_host):
            return hit[1]

    class _NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedir)
    for _ in range(5):
        probe_hdrs = dict(cur_hdrs, Range='bytes=0-0')
        req = urllib.request.Request(cur, headers=probe_hdrs)
        code = 0
        loc = ''
        try:
            with opener.open(req, timeout=3.5) as resp:
                code = getattr(resp, 'status', 200)
                loc = resp.headers.get('Location', '')
        except urllib.error.HTTPError as he:
            code = he.code
            loc = he.headers.get('Location', '')
            he.close()
        except Exception:
            return ''
        if code not in (301, 302, 303, 307, 308):
            with _SAFE_FFMPEG_URL_LOCK:
                if len(_SAFE_FFMPEG_URL_CACHE) >= 32 and cache_key not in _SAFE_FFMPEG_URL_CACHE:
                    _SAFE_FFMPEG_URL_CACHE.pop(next(iter(_SAFE_FFMPEG_URL_CACHE)), None)
                _SAFE_FFMPEG_URL_CACHE[cache_key] = (time.monotonic(), cur)
            return cur
        if not loc:
            return ''
        nxt = urllib.parse.urljoin(cur, loc.strip())
        if not _is_safe_stream_url(nxt):
            return ''
        old_sp = urllib.parse.urlsplit(cur)
        new_sp = urllib.parse.urlsplit(nxt)
        if old_sp.scheme == 'https' and new_sp.scheme != 'https':
            return ''
        if not _has_only_global_ips(new_sp.hostname or ''):
            return ''
        if _cookie_root(old_sp.hostname or '') != _cookie_root(new_sp.hostname or ''):
            for hk in [k for k in list(cur_hdrs.keys()) if k.lower() in ('cookie', 'authorization')]:
                cur_hdrs.pop(hk, None)
        cur = nxt
    return ''


def _is_allowed_local_audio(url: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        rp = Path(url).resolve()
        if not rp.is_file() or rp.suffix.lower() not in AUDIO_EXTS:
            return False
        for d in LOCAL_DIRS:
            try:
                if d.exists() and rp.is_relative_to(d.resolve()):
                    return True
            except OSError:
                continue
    except (OSError, ValueError):
        return False
    return False
_BT_PAUSE_CODE = (
    'import dbus; bus = dbus.SystemBus(private=True)\n'
    'try:\n'
    '    om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")\n'
    '    for p, ifs in om.GetManagedObjects().items():\n'
    '        mp = ifs.get("org.bluez.MediaPlayer1")\n'
    '        if mp and mp.get("Status") == "playing":\n'
    '            dbus.Interface(bus.get_object("org.bluez", p), "org.bluez.MediaPlayer1").Pause()\n'
    'finally:\n'
    '    bus.close()\n'
)


def load_config() -> dict:
    global _CFG_CACHE, _CFG_SIG
    with _CFG_LOCK:
        try:
            st = os.lstat(CONFIG_PATH)
            if not stat.S_ISREG(st.st_mode):
                raise OSError('Refusing non-regular config')
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if _CFG_CACHE is not None and _CFG_SIG == sig:
            return dict(_CFG_CACHE)
        try:
            if sig is not None:
                loaded = json.loads(_read_regular_text(CONFIG_PATH, 262144, private=True))
                if isinstance(loaded, dict):
                    _CFG_CACHE = loaded
                    _CFG_SIG = sig
                    return dict(_CFG_CACHE)
        except Exception as e:
            log.warning('Failed to read config: %s', e)
        _CFG_CACHE = {
            'uin': '',
            'qm_keyst': '',
            'cookies_str': '',
            'quality_level': 'lossless',
            'user_profile': None,
        }
        _CFG_SIG = sig
        return dict(_CFG_CACHE)


_DIR201_CACHE = None
_DIR201_LOCK = threading.Lock()


def save_config(cfg: dict):
    global _CFG_CACHE, _CFG_SIG, _DIR201_CACHE, _LAST_REFRESH_FAIL
    auth_changed = True
    with _CFG_LOCK:
        if isinstance(_CFG_CACHE, dict):
            auth_changed = any(cfg.get(k) != _CFG_CACHE.get(k) for k in ('uin', 'qm_keyst', 'cookies_str', 'quality_level'))
        _CFG_CACHE = dict(cfg)
        tmp = CONFIG_PATH.parent / f'.{CONFIG_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp'
        created = False
        try:
            CONFIG_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                pst = os.lstat(CONFIG_PATH.parent)
                if stat.S_ISDIR(pst.st_mode) and pst.st_uid == os.getuid():
                    os.chmod(CONFIG_PATH.parent, 0o700)
            if CONFIG_PATH.is_symlink():
                raise OSError('Refusing symlink config path')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(fd, 'w', encoding='utf-8') as tf:
                os.fchmod(tf.fileno(), 0o600)
                tf.write(json.dumps(cfg, ensure_ascii=False, indent=2))
            if CONFIG_PATH.is_symlink():
                raise OSError('Refusing symlink config path')
            os.replace(tmp, CONFIG_PATH)
            st = CONFIG_PATH.stat()
            _CFG_SIG = (st.st_ino, st.st_mtime_ns, st.st_size)
        except Exception as e:
            log.error('Failed to save config: %s', e)
        finally:
            if created:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    if auth_changed:
        _LAST_REFRESH_FAIL = (None, 0.0)
        with _DIR201_LOCK:
            _DIR201_CACHE = None


def _is_wx_login(cfg: dict, extra_cookie: str = '') -> bool:
    mk = str(cfg.get('qm_keyst') or '')
    ck = extra_cookie or str(cfg.get('cookies_str') or '')
    prof = cfg.get('user_profile') if isinstance(cfg.get('user_profile'), dict) else {}
    return mk.startswith('W_X_') or prof.get('loginType') == 'wx' or 'wxuin=' in ck or 'tmeLoginType=1' in ck


def _qq_headers(extra_cookie: str = '') -> dict:
    cfg = load_config()
    ck = extra_cookie or cfg.get('cookies_str', '')
    is_wx = _is_wx_login(cfg, ck)
    if cfg.get('uin') and cfg.get('qm_keyst'):
        mk = cfg['qm_keyst']
        if 'qm_keyst=' in ck:
            ck = re.sub(r'qm_keyst=[^;\s]+', lambda _: f'qm_keyst={mk}', ck)
            ck = re.sub(r'qqmusic_key=[^;\s]+', lambda _: f'qqmusic_key={mk}', ck) if 'qqmusic_key=' in ck else f'{ck}; qqmusic_key={mk}'
        else:
            mk_part = (
                f"wxuin={cfg['uin']}; uin={cfg['uin']}; qm_keyst={mk}; qqmusic_key={mk}; tmeLoginType=1"
                if is_wx else f"uin={cfg['uin']}; qm_keyst={mk}; qqmusic_key={mk}"
            )
            ck = f"{ck}; {mk_part}" if ck else mk_part
    h = {
        'User-Agent': UA,
        'Referer': 'https://y.qq.com/',
        'Origin': 'https://y.qq.com',
        'Content-Type': 'application/json',
    }
    if ck:
        h['Cookie'] = ck
    return h


def _qq_comm() -> dict:
    cfg = load_config()
    uin = str(cfg.get('uin') or '0')
    comm = {'ct': 24, 'cv': 0, 'uin': uin, 'format': 'json'}
    if cfg.get('qm_keyst'):
        comm['authst'] = cfg['qm_keyst']
        comm['qqmusic_key'] = cfg['qm_keyst']
        comm['qm_keyst'] = cfg['qm_keyst']
        if _is_wx_login(cfg):
            comm['tmeLoginType'] = 1
    return comm


def _hash33(s: str) -> int:
    e = 0
    for ch in s:
        e += (e << 5) + ord(ch)
    return 2147483647 & e


def _gtk(skey: str) -> int:
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


# ---- QQ & WeChat Official QR Code Login (支持手机QQ / 微信 / QQ音乐App 扫码) ----

def qq_qr_create(mode: str = 'qq') -> dict:
    mode = (mode if isinstance(mode, str) and mode else 'qq').lower()
    if mode == 'wx':
        wx_url = (
            'https://open.weixin.qq.com/connect/qrconnect?appid=wx48db31d50e334801'
            '&redirect_uri=https%3A%2F%2Fy.qq.com%2Fportal%2Fwx_redirect.html%3Flogin_type%3D2'
            '%26surl%3Dhttps%253A%252F%252Fy.qq.com%252F&response_type=code&scope=snsapi_login'
            '&state=STATE#wechat_redirect'
        )
        req = urllib.request.Request(wx_url, headers={'User-Agent': UA})
        try:
            with _safe_urlopen(req, timeout=6) as r:
                html = _read_http_bytes(r).decode('utf-8', errors='replace')
            m = re.search(r'/connect/qrcode/([a-zA-Z0-9_-]+)', html)
            if not m:
                return {'ok': False, 'error': '未获取到微信二维码 uuid'}
            uuid = m.group(1)
            img_url = f'https://open.weixin.qq.com/connect/qrcode/{uuid}'
            with _safe_urlopen(urllib.request.Request(img_url, headers={'User-Agent': UA}), timeout=6) as ir:
                img_b64 = 'data:image/jpeg;base64,' + base64.b64encode(_read_http_bytes(ir)).decode('ascii')
            log.info('Created WeChat QR code: uuid=%s', uuid)
            return {'ok': True, 'mode': 'wx', 'qrsig': uuid, 'qr_img': img_b64}
        except Exception as e:
            log.warning('WeChat QR create failed: %s', e)
            return {'ok': False, 'error': '微信二维码生成失败，请点击刷新重试'}

    # Default: QQ Official QR Login
    xlogin_url = (
        'https://xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=716027609&daid=383&style=33'
        '&login_text=%E7%99%BB%E5%BD%95&hide_title_bar=1&hide_border=1&target=self'
        '&s_url=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump&pt_3rd_aid=100497308'
    )
    qr_url = (
        'https://ssl.ptlogin2.qq.com/ptqrshow?'
        'appid=716027609&e=2&l=M&s=4&d=72&v=4&t=0.5&daid=383&pt_3rd_aid=100497308'
    )
    try:
        pt_login_sig = ''
        with _safe_urlopen(urllib.request.Request(xlogin_url, headers={'User-Agent': UA}), timeout=5) as xr:
            x_ck = '\n'.join(xr.headers.get_all('Set-Cookie') or [])
            ms = re.search(r'pt_login_sig=([^;\s]+)', x_ck)
            if ms:
                pt_login_sig = ms.group(1)

        req = urllib.request.Request(qr_url, headers={'User-Agent': UA, 'Referer': xlogin_url})
        with _safe_urlopen(req, timeout=6) as r:
            png_bytes = _read_http_bytes(r)
            cookies = '\n'.join(r.headers.get_all('Set-Cookie') or [])
            m = re.search(r'qrsig=([^;\s]+)', cookies)
            qrsig = m.group(1) if m else ''
        if not qrsig:
            return {'ok': False, 'error': '未获取到 QQ 二维码签名'}
        now_ts = time.monotonic()
        with _QR_LOCK:
            for old_k in [k for k, v in list(_QR_SESSIONS.items()) if now_ts - v.get('time', 0) > 600]:
                _QR_SESSIONS.pop(old_k, None)
            _QR_SESSIONS[qrsig] = {'pt_login_sig': pt_login_sig, 'time': now_ts}
        b64 = 'data:image/png;base64,' + base64.b64encode(png_bytes).decode('ascii')
        log.info('Created QQ QR code: qrsig=%s...', qrsig[:12])
        return {'ok': True, 'mode': 'qq', 'qrsig': qrsig, 'qr_img': b64}
    except Exception as e:
        log.warning('QQ QR create failed: %s', e)
        return {'ok': False, 'error': 'QQ二维码生成失败，请点击刷新重试'}


def _exchange_code_for_musickey(code: str, appid: str = '', fallback_uin: str = '', fallback_nick: str = '', extra_cookies: dict = None) -> dict:
    extra_cookies = dict(extra_cookies or {})
    p_skey = extra_cookies.get('p_skey', '') or extra_cookies.get('skey', '')
    gtk_val = _gtk(p_skey) if p_skey else 5381

    if appid:
        # WeChat Login (login_type == 2)
        login_payload = {
            'comm': {'g_tk': gtk_val, 'platform': 'yqq', 'ct': 24, 'cv': 0, 'tmeAppID': 'qqmusic', 'tmeLoginType': '1'},
            'req': {
                'module': 'music.login.LoginServer',
                'method': 'Login',
                'param': {'code': code, 'strAppid': appid},
            },
        }
    else:
        # QQ Login (login_type == 1, matches y.qq.com/portal/wx_redirect.html)
        login_payload = {
            'comm': {'g_tk': gtk_val, 'platform': 'yqq', 'ct': 24, 'cv': 0},
            'req': {
                'module': 'QQConnectLogin.LoginServer',
                'method': 'QQLogin',
                'param': {'code': code},
            },
        }

    qm_keyst = ''
    uin = fallback_uin
    nick = fallback_nick
    headurl = f'https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100' if uin else ''
    if code:
        try:
            ck_hdr = '; '.join(f'{k}={v}' for k, v in extra_cookies.items() if v)
            hdrs = {'User-Agent': UA, 'Referer': 'https://y.qq.com/portal/wx_redirect.html', 'Content-Type': 'application/x-www-form-urlencoded'}
            if ck_hdr:
                hdrs['Cookie'] = ck_hdr
            lreq = urllib.request.Request(
                'https://u.y.qq.com/cgi-bin/musicu.fcg',
                data=json.dumps(login_payload).encode('utf-8'),
                headers=hdrs,
            )
            with _safe_urlopen(lreq, timeout=6) as lr:
                for sc in lr.headers.get_all('Set-Cookie') or []:
                    part = sc.split(';', 1)[0].strip()
                    if '=' in part:
                        k, v = part.split('=', 1)
                        if v:
                            extra_cookies[k] = v
                ldata = json.loads(_read_http_bytes(lr).decode('utf-8', errors='replace'))
                rinfo = (ldata.get('req') or {}).get('data') or {}
                qm_keyst = rinfo.get('musickey') or extra_cookies.get('qm_keyst') or extra_cookies.get('qqmusic_key') or ''
                if rinfo.get('str_musicid') or rinfo.get('musicid'):
                    uin = str(rinfo.get('str_musicid') or rinfo.get('musicid'))
                if rinfo.get('nick'):
                    nick = rinfo['nick']
                if rinfo.get('logo') or rinfo.get('headurl'):
                    headurl = rinfo.get('logo') or rinfo.get('headurl')
                log.info('OAuth exchange code=%s musickey_len=%d uin=%s nick=%s',
                         (ldata.get('req') or {}).get('code'), len(qm_keyst), uin, nick)
        except Exception as e:
            log.warning('LoginServer exchange error: %s', e)

    if not uin and not qm_keyst:
        log.warning('OAuth exchange produced neither uin nor qm_keyst; aborting config overwrite')
        return None

    all_cookies = dict(extra_cookies)
    if uin:
        all_cookies['uin'] = uin
        all_cookies['wxuin'] = uin
    if qm_keyst:
        all_cookies['qm_keyst'] = qm_keyst
        all_cookies['qqmusic_key'] = qm_keyst

    profile = {
        'userId': uin or 'vip_user',
        'nickname': nick or f'QQ音乐用户_{uin}',
        'avatarUrl': headurl,
        'isVip': bool(qm_keyst),
        'loginType': 'wx' if appid else 'qq',
    }
    with _CFG_LOCK:
        cfg = load_config()
        cfg['uin'] = uin
        cfg['qm_keyst'] = qm_keyst
        cfg['cookies_str'] = '; '.join(f'{k}={v}' for k, v in all_cookies.items() if v)
        cfg['user_profile'] = profile
        save_config(cfg)
    _clear_dir201_cache()
    log.info('Saved QQ Music login profile: %s (uin=%s)', profile['nickname'], uin)
    return profile


def _complete_qq_oauth(check_sig_url: str, nickname_hint: str = '') -> dict:
    parts = urllib.parse.urlsplit(str(check_sig_url or '').strip())
    host = (parts.hostname or '').lower().rstrip('.')
    if parts.scheme != 'https' or parts.username is not None or not (host == 'qq.com' or host.endswith('.qq.com')):
        raise ValueError(f'Refusing untrusted check_sig URL host: {host!r}')
    if urllib.request.urlopen is _ORIG_URLOPEN and not _has_only_global_ips(host):
        raise ValueError(f'Refusing non-global check_sig URL host: {host!r}')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
    opener.addheaders = [('User-Agent', UA), ('Referer', 'https://xui.ptlogin2.qq.com/')]

    raw_set_cookies = []
    try:
        with opener.open(check_sig_url, timeout=6) as r:
            raw_set_cookies.extend(r.headers.get_all('Set-Cookie') or [])
    except urllib.error.HTTPError as he:
        raw_set_cookies.extend(he.headers.get_all('Set-Cookie') or [])
        he.close()

    cookie_dict = {c.name: c.value for c in cj if c.value}
    for sc in raw_set_cookies:
        part = sc.split(';', 1)[0].strip()
        if '=' in part:
            k, v = part.split('=', 1)
            if v:
                cookie_dict[k] = v

    uin = re.sub(r'^o0*', '', cookie_dict.get('p_uin', '') or cookie_dict.get('uin', ''))
    p_skey = cookie_dict.get('p_skey', '') or cookie_dict.get('skey', '')
    log.info('check_sig cookies: uin=%s has_p_skey=%s keys=%s', uin, bool(p_skey), list(cookie_dict.keys()))

    code = ''
    if p_skey:
        auth_data = urllib.parse.urlencode({
            'response_type': 'code',
            'client_id': '100497308',
            'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/',
            'scope': 'get_user_info,get_app_friends',
            'state': 'state',
            'switch': '',
            'from_ptlogin': '1',
            'src': '1',
            'update_auth': '1',
            'openapi': '1010_1030',
            'g_tk': str(_gtk(p_skey)),
            'auth_time': str(int(time.time() * 1000)),
            'ui': '00000000-0000-0000-0000-000000000000',
        }).encode('utf-8')
        cookie_hdr = '; '.join(f'{k}={v}' for k, v in cookie_dict.items())
        loc = ''
        try:
            if urllib.request.urlopen is _ORIG_URLOPEN and not _has_only_global_ips('graph.qq.com'):
                raise ValueError('Refusing non-global graph.qq.com host')
            req_auth = urllib.request.Request(
                'https://graph.qq.com/oauth2.0/authorize',
                data=auth_data,
                headers={
                    'User-Agent': UA,
                    'Referer': 'https://graph.qq.com/oauth2.0/show',
                    'Cookie': cookie_hdr,
                },
            )
            with opener.open(req_auth, timeout=6) as r_auth:
                loc = r_auth.geturl()
        except urllib.error.HTTPError as he:
            loc = he.headers.get('Location', '')
            he.close()
        log.info('graph.qq.com authorize redirect Location: %s', re.sub(r'([?&]code=)[^&#]+', r'\1***', loc)[:120])
        m_code = re.search(r'[?&]code=([^&#]+)', loc)
        if m_code:
            code = m_code.group(1)

    return _exchange_code_for_musickey(code, appid='', fallback_uin=uin, fallback_nick=nickname_hint, extra_cookies=cookie_dict)


def qq_qr_check(qrsig: str, mode: str = 'qq') -> dict:
    if not isinstance(qrsig, str) or not qrsig.strip():
        return {'ok': False, 'error': '缺少 qrsig'}
    qrsig = qrsig.strip()
    if len(qrsig) > 512 or any(ord(c) < 0x20 or ord(c) == 0x7f or c in ';\r\n' for c in qrsig):
        return {'ok': False, 'error': '无效的 qrsig'}
    mode = (mode if isinstance(mode, str) and mode else 'qq').lower()

    if mode == 'wx':
        q_uuid = urllib.parse.quote(qrsig, safe='')
        lp_url = f'https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={q_uuid}&_={int(time.time() * 1000)}'
        req = urllib.request.Request(lp_url, headers={'User-Agent': UA, 'Referer': 'https://open.weixin.qq.com/'})
        try:
            with _safe_urlopen(req, timeout=2.5) as r:
                txt = _read_http_bytes(r).decode('utf-8', errors='replace').strip()
            m_err = re.search(r'wx_errcode=(\d+)', txt)
            m_code = re.search(r"wx_code='([^']*)'", txt)
            errcode = int(m_err.group(1)) if m_err else 408
            wx_code = m_code.group(1) if m_code else ''
            log.info('WeChat QR poll uuid=%s errcode=%s has_code=%s', qrsig, errcode, bool(wx_code))
            if errcode == 405 and wx_code:
                profile = _exchange_code_for_musickey(wx_code, appid='wx48db31d50e334801')
                if not profile:
                    return {'ok': False, 'error': '微信登录授权失败，请点击刷新二维码重试'}
                return {'ok': True, 'code': 0, 'message': '微信登录成功', 'profile': profile}
            if errcode == 404:
                return {'ok': True, 'code': 67, 'message': '微信已扫码，请在手机微信上点击确认'}
            if errcode in (402, 403, 500):
                return {'ok': True, 'code': 65, 'message': '微信二维码已过期，请点击刷新'}
            return {'ok': True, 'code': 66, 'message': '等待微信扫码...'}
        except TimeoutError:
            return {'ok': True, 'code': 66, 'message': '等待微信扫码...'}
        except urllib.error.URLError as ue:
            if 'timed out' in str(ue).lower():
                return {'ok': True, 'code': 66, 'message': '等待微信扫码...'}
            log.warning('WeChat QR poll URLError: %s', ue)
            return {'ok': False, 'error': '二维码连接波动，请点击刷新重试'}
        except Exception as e:
            log.warning('WeChat QR poll error: %s', e)
            return {'ok': False, 'error': '二维码服务暂时不可用，请点击刷新重试'}

    # QQ QR Check
    with _QR_LOCK:
        sess = dict(_QR_SESSIONS.get(qrsig) or {})
    pt_login_sig = str(sess.get('pt_login_sig') or '').translate({10: None, 13: None, 59: None})
    q_login_sig = urllib.parse.quote(pt_login_sig, safe='')
    token = _hash33(qrsig)
    chk_url = (
        f'https://ssl.ptlogin2.qq.com/ptqrlogin?u1=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump'
        f'&ptqrtoken={token}&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052'
        f'&action=0-0-{int(time.time() * 1000)}&js_ver=24032716&js_type=1'
        f'&login_sig={q_login_sig}&pt_uistyle=40&aid=716027609&daid=383&pt_3rd_aid=100497308'
    )
    ck = f'qrsig={qrsig}' + (f'; pt_login_sig={pt_login_sig}' if pt_login_sig else '')
    req = urllib.request.Request(
        chk_url,
        headers={
            'User-Agent': UA,
            'Referer': 'https://xui.ptlogin2.qq.com/',
            'Cookie': ck,
        },
    )
    try:
        with _safe_urlopen(req, timeout=6) as r:
            txt = _read_http_bytes(r).decode('utf-8', errors='replace').strip()
        parts = re.findall(r"'([^']*)'", txt)
        if len(parts) < 5 or not parts[0].isdigit():
            log.info('QQ QR unexpected response: %s', txt[:120])
            return {'ok': True, 'code': -1, 'message': '二维码状态异常，请点击刷新重试'}
        code = int(parts[0])
        jump_url = parts[2]
        msg = parts[4]
        nick = parts[5] if len(parts) > 5 else ''
        if code != 66:
            log.info('QQ QR poll code=%d msg=%s nick=%s jump=%s', code, msg, nick, bool(jump_url))
        if code == 0 and jump_url:
            with _QR_LOCK:
                _QR_SESSIONS.pop(qrsig, None)
            profile = _complete_qq_oauth(jump_url, nick)
            if not profile:
                return {'ok': False, 'error': 'QQ登录授权失败，请点击刷新二维码重试'}
            return {'ok': True, 'code': 0, 'message': '登录成功', 'profile': profile}
        if code == 65:
            with _QR_LOCK:
                _QR_SESSIONS.pop(qrsig, None)
        return {'ok': True, 'code': code, 'message': msg, 'nickname': nick}
    except Exception as e:
        log.warning('qq_qr_check error: %s', e)
        return {'ok': False, 'error': '二维码服务暂时不可用，请点击刷新重试'}


def qq_bind_uin(raw_input: str) -> dict:
    """Bind a QQ Number (UIN) or full y.qq.com Cookie string."""
    if isinstance(raw_input, int) and not isinstance(raw_input, bool):
        raw_input = str(raw_input)
    raw = raw_input.strip() if isinstance(raw_input, str) else ''
    if not raw:
        return {'ok': False, 'error': '请输入 QQ 号或 Cookie'}
    with _CFG_LOCK:
        cfg = load_config()
        if raw.isascii() and raw.isdigit():
            uin = raw.lstrip('0')
            if not (5 <= len(uin) <= 20):
                return {'ok': False, 'error': '请输入 5～20 位有效账号'}
            if str(cfg.get('uin') or '') != uin:
                cfg['qm_keyst'] = ''
                cfg['cookies_str'] = ''
            profile = {
                'userId': uin,
                'nickname': f'QQ音乐用户 ({uin})',
                'avatarUrl': f'https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100' if len(uin) <= 12 else '',
                'isVip': bool(cfg.get('qm_keyst')),
            }
            cfg['uin'] = uin
            cfg['user_profile'] = profile
            save_config(cfg)
            _clear_dir201_cache()
            return {'ok': True, 'profile': profile}

        # Parse full Cookie string (containing uin=...; qm_keyst=...) — strip control/non-ASCII header chars
        clean_raw = re.sub(r'[\x00-\x1f\x7f-\uffff]+', '', raw).strip()
        m_uin = re.search(r'(?:uin|wxuin)=o?0*([1-9][0-9]{4,19})(?=[;\s]|$)', clean_raw)
        m_key = re.search(r'(?:qm_keyst|qqmusic_key)=([^;\s]{1,1024})', clean_raw)
        if not m_key and not re.fullmatch(r'[A-Za-z0-9_=-]{16,512}', clean_raw):
            return {'ok': False, 'error': '请输入有效 QQ 号或包含 qm_keyst 的 Cookie'}
        prev_uin = str(cfg.get('uin') or '').strip()
        uin = m_uin.group(1) if m_uin else (prev_uin if prev_uin.isascii() and prev_uin.isdigit() else '10000')
        qm_keyst = m_key.group(1) if m_key else clean_raw
        is_wx = qm_keyst.startswith('W_X_') or 'wxuin=' in clean_raw or 'tmeLoginType=1' in clean_raw
        profile = {
            'userId': uin,
            'nickname': f'QQ音乐绿钻VIP ({uin})',
            'avatarUrl': f'https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100' if (not is_wx and uin.isascii() and uin.isdigit() and len(uin) <= 12) else '',
            'isVip': True,
            'loginType': 'wx' if is_wx else 'qq',
        }
        cfg['uin'] = uin
        cfg['qm_keyst'] = qm_keyst
        if '=' in clean_raw:
            cfg['cookies_str'] = clean_raw[:4096]
        elif is_wx:
            cfg['cookies_str'] = f'wxuin={uin}; uin={uin}; qm_keyst={qm_keyst}; qqmusic_key={qm_keyst}; tmeLoginType=1'
        else:
            cfg['cookies_str'] = f'uin={uin}; qm_keyst={qm_keyst}; qqmusic_key={qm_keyst}'
        cfg['user_profile'] = profile
        save_config(cfg)
        _clear_dir201_cache()
        return {'ok': True, 'profile': profile}


def _clear_dir201_cache():
    global _DIR201_CACHE
    with _DIR201_LOCK:
        _DIR201_CACHE = None


def qq_logout() -> dict:
    with _CFG_LOCK:
        cfg = load_config()
        cfg['uin'] = ''
        cfg['qm_keyst'] = ''
        cfg['cookies_str'] = ''
        cfg['user_profile'] = None
        save_config(cfg)
    _clear_dir201_cache()
    return {'ok': True}


# ---- QQ Music Data & Stream Resolution ----

def _default_quality_label(cfg: dict = None) -> str:
    q = (cfg if isinstance(cfg, dict) else load_config()).get('quality_level', 'lossless')
    if q == 'lossless':
        return 'SQ 无损 FLAC'
    if q == 'standard':
        return 'AAC 极高音质'
    return 'HQ 320k MP3'


def _format_qq_song(s: dict, purl_map: dict = None, default_q: str = None) -> dict:
    mid = s.get('mid') or s.get('songmid') or ''
    media_mid = (s.get('file') or {}).get('media_mid') or s.get('strMediaMid') or mid
    title = (s.get('name') or s.get('songname') or '').strip()
    singers = s.get('singer') or []
    artist = '/'.join(x.get('name', '') for x in singers if x.get('name'))
    album_obj = s.get('album') or {}
    album_name = (album_obj.get('name') or s.get('albumname') or '').strip()
    albummid = album_obj.get('mid') or s.get('albummid') or ''
    cover = f'https://y.gtimg.cn/music/photo_new/T002R500x500M000{albummid}.jpg' if albummid else ''
    thumb = f'https://y.gtimg.cn/music/photo_new/T002R150x150M000{albummid}.jpg' if albummid else ''
    duration = int(s.get('interval') or 0)
    pay_obj = s.get('pay') or {}
    pay_play = pay_obj.get('pay_play') or pay_obj.get('payplay', 0)

    url_info = (purl_map or {}).get(mid) or {}
    if default_q is None:
        default_q = _default_quality_label()
    return {
        'mid': mid,
        'media_mid': media_mid,
        'title': title,
        'artist': artist,
        'album': album_name,
        'albummid': albummid,
        'cover': cover,
        'thumb': thumb,
        'duration': duration,
        'vip': bool(pay_play),
        'source': url_info.get('source', 'QQ音乐·SQ无损'),
        'quality': url_info.get('quality', default_q),
        'url': url_info.get('url'),
        'headers': {'User-Agent': UA, 'Referer': 'https://y.qq.com/'},
        'is_local': False,
    }


def _refresh_musickey_from_cookies() -> bool:
    """Silently refresh qm_keyst (musickey) from saved QQ OAuth cookies when needed."""
    global _LAST_REFRESH_FAIL
    with _REFRESH_LOCK:
        cfg = load_config()
        cookies = str(cfg.get('cookies_str') or '')
        uin = str(cfg.get('uin') or '')
        m = re.search(r'p_skey=([^;\s]+)', cookies)
        if not m or not uin:
            return False
        p_skey = m.group(1)
        key_id = (uin, p_skey)
        if _LAST_REFRESH_FAIL[0] == key_id and (time.monotonic() - _LAST_REFRESH_FAIL[1]) < 60.0:
            return False
        if urllib.request.urlopen is _ORIG_URLOPEN and not _has_only_global_ips('graph.qq.com'):
            _LAST_REFRESH_FAIL = (key_id, time.monotonic())
            return False
        g_tk = _gtk(p_skey)

        class StopRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(StopRedir())
        data = urllib.parse.urlencode({
            'response_type': 'code',
            'client_id': '100497308',
            'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/',
            'scope': 'get_user_info,get_app_friends',
            'state': 'state',
            'switch': '',
            'from_ptlogin': '1',
            'src': '1',
            'update_auth': '1',
            'openapi': '1010_1030',
            'g_tk': str(g_tk),
            'auth_time': str(int(time.time() * 1000)),
            'ui': uin,
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://graph.qq.com/oauth2.0/authorize',
            data=data,
            headers={
                'User-Agent': UA,
                'Cookie': cookies,
                'Referer': 'https://graph.qq.com/oauth2.0/show?which=Login&display=pc&response_type=code&client_id=100497308&redirect_uri=https%3A%2F%2Fy.qq.com%2Fportal%2Fwx_redirect.html%3Flogin_type%3D1%26surl%3Dhttps%3A%2F%2Fy.qq.com%2F&state=state',
            },
        )
        loc = ''
        try:
            with opener.open(req, timeout=5) as resp:
                loc = resp.headers.get('Location', '')
        except urllib.error.HTTPError as e:
            loc = e.headers.get('Location', '')
            e.close()
        except Exception:
            _LAST_REFRESH_FAIL = (key_id, time.monotonic())
            return False

        mc = re.search(r'code=([A-Za-z0-9_-]+)', loc)
        if not mc:
            _LAST_REFRESH_FAIL = (key_id, time.monotonic())
            return False
        payload = {
            'comm': {'g_tk': g_tk, 'platform': 'yqq', 'ct': 24, 'cv': 0},
            'req': {
                'module': 'QQConnectLogin.LoginServer',
                'method': 'QQLogin',
                'param': {'code': mc.group(1)},
            },
        }
        try:
            req2 = urllib.request.Request(
                'https://u.y.qq.com/cgi-bin/musicu.fcg',
                data=json.dumps(payload).encode('utf-8'),
                headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Cookie': cookies},
            )
            with _safe_urlopen(req2, timeout=5) as r2:
                body = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
                d = (body.get('req') or {}).get('data') or {}
                mk = str(d.get('musickey') or '').strip()
                if mk and re.fullmatch(r'[A-Za-z0-9_.=-]{4,1024}', mk):
                    with _CFG_LOCK:
                        cur_cfg = load_config()
                        cur_ck = str(cur_cfg.get('cookies_str') or '')
                        cur_m = re.search(r'p_skey=([^;\s]+)', cur_ck)
                        if str(cur_cfg.get('uin') or '') != uin or not cur_m or cur_m.group(1) != p_skey:
                            log.info('Skipping musickey save because account changed or logged out during refresh')
                            return False
                        if 'qm_keyst=' in cur_ck:
                            cur_ck = re.sub(r'qm_keyst=[^;\s]+', lambda _: f'qm_keyst={mk}', cur_ck)
                        if 'qqmusic_key=' in cur_ck:
                            cur_ck = re.sub(r'qqmusic_key=[^;\s]+', lambda _: f'qqmusic_key={mk}', cur_ck)
                        cur_cfg['qm_keyst'] = mk
                        cur_cfg['cookies_str'] = cur_ck
                        save_config(cur_cfg)
                    log.info('Silently refreshed QQ Music VIP musickey (len=%d)', len(mk))
                    return True
        except Exception as e:
            log.warning('Musickey refresh failed: %s', e)
        _LAST_REFRESH_FAIL = (key_id, time.monotonic())
        return False


def resolve_qq_vkey(mid: str, media_mid: str = '', _retried_refresh: bool = False) -> dict:
    """Resolve official QQ Music stream URL in a single batched call (prioritizing SQ Lossless FLAC)."""
    if not mid:
        return None
    mmid = media_mid or mid
    cfg = load_config()
    uin = str(cfg.get('uin') or '10000')
    q_mode = cfg.get('quality_level', 'lossless')

    candidates = []
    if q_mode == 'lossless':
        candidates.append((f'F000{mmid}.flac', 'SQ 无损 FLAC'))
        candidates.append((f'M800{mmid}.mp3', 'HQ 320k MP3'))
    elif q_mode == 'hq':
        candidates.append((f'M800{mmid}.mp3', 'HQ 320k MP3'))
        candidates.append((f'F000{mmid}.flac', 'SQ 无损 FLAC'))
    candidates.append((f'C400{mmid}.m4a', 'AAC 极高音质'))
    candidates.append((f'M500{mmid}.mp3', '标准音质'))

    filenames = [c[0] for c in candidates]
    payload = {
        'comm': _qq_comm(),
        'req_0': {
            'module': 'vkey.GetVkeyServer',
            'method': 'CgiGetVkey',
            'param': {
                'guid': '10000',
                'songmid': [mid] * len(filenames),
                'filename': filenames,
                'songtype': [0] * len(filenames),
                'uin': uin,
                'loginflag': 1,
                'platform': '20',
            },
        },
    }
    try:
        req = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            data=json.dumps(payload).encode('utf-8'),
            headers=_qq_headers(),
        )
        with _safe_urlopen(req, timeout=5) as r:
            vdata = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
            d0 = vdata.get('req_0', {}).get('data', {})
            sips = d0.get('sip') if isinstance(d0.get('sip'), list) else []
            sip = next(
                (u.strip() for u in sips if isinstance(u, str) and _is_safe_stream_url(u.strip())),
                'https://dl.stream.qqmusic.qq.com/',
            )
            if not sip.endswith('/'):
                sip += '/'
            purl_by_fn = {
                info.get('filename'): info.get('purl')
                for info in (d0.get('midurlinfo') or [])
                if info.get('purl')
            }
            for filename, q_label in candidates:
                if purl_by_fn.get(filename):
                    cand_url = sip + str(purl_by_fn[filename]).lstrip('/')
                    if not _is_safe_stream_url(cand_url):
                        continue
                    log.info('Resolved %s (%s) -> %s', mid, q_label, filename)
                    return {
                        'url': cand_url,
                        'quality': q_label,
                        'source': f'QQ音乐·{q_label}',
                        '_url_ts': time.monotonic(),
                        'headers': {'User-Agent': UA, 'Referer': 'https://y.qq.com/'},
                    }
    except Exception as e:
        log.warning('resolve_qq_vkey error: %s', e)

    if not _retried_refresh and _refresh_musickey_from_cookies():
        return resolve_qq_vkey(mid, media_mid, _retried_refresh=True)
    return None


_BILI_CJ = None
_BILI_CJ_TS = 0.0
_BILI_CJ_LOCK = threading.Lock()


def resolve_hires_fallback(query: str, title_hint: str = '', artist_hint: str = '') -> dict:
    """High-bitrate full-track stream resolver when a track is VIP-locked without a VIP key."""
    global _BILI_CJ, _BILI_CJ_TS
    try:
        if urllib.request.urlopen is _ORIG_URLOPEN and (
            not _has_only_global_ips('www.bilibili.com') or not _has_only_global_ips('api.bilibili.com')
        ):
            return None
        now = time.monotonic()
        with _BILI_CJ_LOCK:
            cj = _BILI_CJ
            need_init = cj is None or (now - _BILI_CJ_TS) > 3600.0
            if need_init:
                cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), _SafeRedirectHandler)
        opener.addheaders = [('User-Agent', UA), ('Referer', 'https://www.bilibili.com/')]
        if need_init:
            with opener.open('https://www.bilibili.com/', timeout=4) as r0:
                r0.read(64)
            with _BILI_CJ_LOCK:
                _BILI_CJ = cj
                _BILI_CJ_TS = time.monotonic()

        q = f'{query} 完整版 无损'
        surl = 'https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=' + urllib.parse.quote(q)
        with opener.open(surl, timeout=4) as r:
            sdata = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
            results = sdata.get('data', {}).get('result', [])
        if not results:
            return None

        t_hint = (title_hint or '').strip().lower()
        a_hint = (artist_hint or '').strip().lower()
        best_item = None
        best_score = -1
        for item in results[:8]:
            dur_str = str(item.get('duration', '0:0'))
            parts = [int(x) for x in dur_str.split(':') if x.isdigit()]
            if len(parts) == 3:
                secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                secs = parts[0] * 60 + parts[1]
            else:
                secs = 240
            if not (60 <= secs <= 720):
                continue
            raw_title = re.sub(r'<[^>]+>', '', str(item.get('title', ''))).lower()
            score = 0
            if t_hint and t_hint in raw_title:
                score += 2
            if a_hint and a_hint in raw_title:
                score += 1
            if score > best_score:
                best_score = score
                best_item = item
        chosen = best_item or results[0]

        bvid = str(chosen.get('bvid') or '').strip()
        if not re.fullmatch(r'BV[0-9A-Za-z]{8,16}', bvid):
            return None
        vurl = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
        with opener.open(vurl, timeout=4) as vr:
            vdata = json.loads(_read_http_bytes(vr).decode('utf-8', errors='replace'))
        raw_cid = (vdata.get('data') or {}).get('cid')
        if isinstance(raw_cid, bool):
            return None
        cid = int(raw_cid)
        if cid <= 0:
            return None
        dur = int((vdata.get('data') or {}).get('duration') or 240)

        purl = f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&fnval=16'
        with opener.open(purl, timeout=4) as pr:
            pdata = json.loads(_read_http_bytes(pr).decode('utf-8', errors='replace'))
        audios = pdata.get('data', {}).get('dash', {}).get('audio', [])
        if not audios:
            return None
        best = sorted((a for a in audios if isinstance(a, dict)), key=lambda x: x.get('bandwidth', 0), reverse=True)
        if not best:
            return None
        best = best[0]
        cand_urls = [
            *(best.get('backupUrl') if isinstance(best.get('backupUrl'), list) else []),
            *(best.get('backup_url') if isinstance(best.get('backup_url'), list) else []),
            best.get('baseUrl'),
            best.get('base_url'),
        ]
        stream_url = next(
            (u.strip() for u in cand_urls if isinstance(u, str) and _is_safe_stream_url(u.strip())),
            '',
        )
        if not stream_url:
            return None
        bw_kbps = best.get('bandwidth', 192000) // 1000
        return {
            'url': stream_url,
            'quality': f'Hi-Res ({bw_kbps}kbps)',
            'source': 'QQ音乐·Hi-Res音源',
            'headers': {'User-Agent': UA, 'Referer': 'https://www.bilibili.com/'},
            'duration': dur,
            '_url_ts': time.monotonic(),
        }
    except Exception as e:
        log.warning('Fallback audio error: %s', e)
        return None


def qq_search(query: str, limit: int = 25, page: int = 1) -> list:
    if isinstance(query, (int, float)) and not isinstance(query, bool):
        query = str(query)
    elif not isinstance(query, str):
        query = ''
    lim = max(1, min(60, int(limit)))
    pg = max(1, min(50, int(page)))
    req_body = {
        'method': 'DoSearchForQQMusicDesktop',
        'module': 'music.search.SearchCgiService',
        'param': {
            'grp': 1,
            'num_per_page': lim,
            'page_num': pg,
            'query': query,
            'search_type': 0,
        },
    }

    def _call_search(use_cookies: bool):
        raw_uin = str(load_config().get('uin') or '0').strip() if use_cookies else '0'
        uin_val = int(raw_uin) if (raw_uin.isascii() and raw_uin.isdigit()) else 0
        payload = {'comm': {'ct': 19, 'cv': 1859, 'uin': uin_val}, 'req': req_body}
        hdrs = _qq_headers() if use_cookies else {
            'User-Agent': UA,
            'Referer': 'https://y.qq.com/',
            'Content-Type': 'application/json',
        }
        r_obj = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            data=json.dumps(payload).encode('utf-8'),
            headers=hdrs,
        )
        with _safe_urlopen(r_obj, timeout=5) as r:
            data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        r_node = data.get('req') or {}
        r_code = r_node.get('code') if 'code' in r_node else data.get('code', 0)
        songs = ((r_node.get('data') or {}).get('body') or {}).get('song', {}).get('list') or []
        return r_code, songs

    def _call_shc_fallback():
        fb_url = (
            'https://shc.y.qq.com/soso/fcgi-bin/search_for_qq_cp'
            f'?format=json&inCharset=utf8&outCharset=utf-8&n={lim}&p={pg}&w='
            + urllib.parse.quote(query, safe='')
        )
        r_fb = urllib.request.Request(fb_url, headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/'})
        with _safe_urlopen(r_fb, timeout=5) as r:
            fb_data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        if fb_data.get('code', 0) != 0:
            return []
        return (((fb_data.get('data') or {}).get('song') or {}).get('list')) or []

    try:
        cfg = load_config()
        has_ck = bool(cfg.get('cookies_str') or cfg.get('qm_keyst'))
        code, songs = _call_search(has_ck)
        if has_ck and (code != 0 or not songs):
            if code in (1000, 2001) and _refresh_musickey_from_cookies():
                code, songs = _call_search(True)
            if code != 0 or not songs:
                code, songs = _call_search(False)
        if code != 0 or not songs:
            songs = _call_shc_fallback()
        dq = _default_quality_label(cfg)
        return [_format_qq_song(s, default_q=dq) for s in songs if isinstance(s, dict) and (s.get('mid') or s.get('songmid'))]
    except Exception as e:
        log.warning('qq_search error: %s', e)
        return []


_SMARTBOX_CACHE = {}
_SMARTBOX_LOCK = threading.Lock()


def _clean_smartbox_query(raw) -> str:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = str(raw)
    elif not isinstance(raw, str):
        return ''
    return re.sub(r'[\x00-\x1f\x7f]+', '', raw).strip()[:48]


def _get_cached_smartbox(q: str):
    if not q:
        return {'ok': True, 'words': [], 'items': []}
    now = time.monotonic()
    with _SMARTBOX_LOCK:
        hit = _SMARTBOX_CACHE.get(q)
        if hit and (now - hit[0]) < 180.0:
            return hit[1]
    return None


def qq_smartbox(query: str) -> dict:
    q = _clean_smartbox_query(query)
    cached = _get_cached_smartbox(q)
    if cached is not None:
        return cached
    url = (
        'https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg'
        '?is_xml=0&format=json&key=' + urllib.parse.quote(q, safe='')
        + '&g_tk=5381&loginUin=0&hostUin=0&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0'
    )
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/'})
    words = []
    seen_words = set()
    items = []
    seen_items = set()

    def _add_word(w: str):
        w = html.unescape(str(w or '')).strip()
        w_base = re.sub(r'\s*[\(（][^\)）]{1,24}[\)）]\s*$', '', w).strip() or w
        for cand in (w_base, w):
            if 1 <= len(cand) <= 24 and cand not in seen_words and len(words) < 12:
                seen_words.add(cand)
                words.append(cand)

    try:
        with _safe_urlopen(req, timeout=2.5) as r:
            data = (json.loads(_read_http_bytes(r).decode('utf-8', errors='replace')).get('data') or {})
        for sg in ((data.get('singer') or {}).get('itemlist') or [])[:4]:
            if not isinstance(sg, dict):
                continue
            name = html.unescape(str(sg.get('name') or '')).strip()[:80]
            if not name:
                continue
            _add_word(name)
            ik = f'singer:{name}'
            if ik not in seen_items and len(items) < 8:
                seen_items.add(ik)
                items.append({'type': 'singer', 'title': name, 'sub': '歌手', 'query': name})
        for so in ((data.get('song') or {}).get('itemlist') or [])[:8]:
            if not isinstance(so, dict):
                continue
            name = html.unescape(str(so.get('name') or '')).strip()[:100]
            singer = html.unescape(str(so.get('singer') or '')).strip()[:80]
            if not name:
                continue
            _add_word(name)
            if singer and '/' not in singer:
                _add_word(singer)
            ik = f'song:{name}:{singer}'
            if ik not in seen_items and len(items) < 8:
                seen_items.add(ik)
                q_str = f'{name} {singer}'.strip()[:120]
                items.append({'type': 'song', 'title': name, 'sub': singer or '单曲', 'query': q_str})
        res = {'ok': True, 'words': words, 'items': items}
        with _SMARTBOX_LOCK:
            if len(_SMARTBOX_CACHE) >= 96:
                _SMARTBOX_CACHE.pop(next(iter(_SMARTBOX_CACHE)), None)
            _SMARTBOX_CACHE[q] = (time.monotonic(), res)
        return res
    except Exception as e:
        log.debug('qq_smartbox error: %s', e)
        return {'ok': True, 'words': [], 'items': []}


def qq_toplist(top_id: int = 26, limit: int = 40) -> list:
    req_1 = {
        'module': 'musicToplist.ToplistInfoServer',
        'method': 'GetDetail',
        'param': {'topId': int(top_id), 'offset': 0, 'num': limit, 'period': ''},
    }
    try:
        cfg = load_config()
        has_ck = bool(cfg.get('cookies_str') or cfg.get('qm_keyst'))
        payload = {'comm': _qq_comm(), 'req_1': req_1}
        req = urllib.request.Request(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            data=json.dumps(payload).encode('utf-8'),
            headers=_qq_headers(),
        )
        with _safe_urlopen(req, timeout=6) as r:
            data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
        r1 = data.get('req_1') or {}
        r1_code = r1.get('code') if 'code' in r1 else data.get('code', 0)
        songs = (r1.get('data') or {}).get('songInfoList') or []
        if has_ck and (r1_code != 0 or not songs):
            if r1_code in (1000, 2001) and _refresh_musickey_from_cookies():
                req_retry = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': _qq_comm(), 'req_1': req_1}).encode('utf-8'),
                    headers=_qq_headers(),
                )
                with _safe_urlopen(req_retry, timeout=6) as rr:
                    dr = json.loads(_read_http_bytes(rr).decode('utf-8', errors='replace'))
                rr1 = dr.get('req_1') or {}
                r1_code = rr1.get('code') if 'code' in rr1 else dr.get('code', 0)
                songs = (rr1.get('data') or {}).get('songInfoList') or songs
            if r1_code != 0 or not songs:
                req_anon = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': {'ct': 24, 'cv': 0}, 'req_1': req_1}).encode('utf-8'),
                    headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'},
                )
                with _safe_urlopen(req_anon, timeout=6) as r2:
                    d2 = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
                songs = ((d2.get('req_1') or {}).get('data') or {}).get('songInfoList') or songs
        dq = _default_quality_label(cfg)
        return [_format_qq_song(s, default_q=dq) for s in songs if s.get('mid')]
    except Exception as e:
        log.warning('qq_toplist error: %s', e)
        return []


_LYRIC_CACHE = {}
_LYRIC_LOCK = threading.Lock()


def qq_lyric(mid: str) -> list:
    """Fetch and parse LRC synchronized lyrics from QQ Music."""
    if not isinstance(mid, str) or not mid.strip():
        return []
    mid = mid.strip()
    with _LYRIC_LOCK:
        cached = _LYRIC_CACHE.get(mid)
    if cached is not None:
        return list(cached)
    q_mid = urllib.parse.quote(mid, safe='')
    url = f'https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={q_mid}&format=json&nobase64=1'
    req = urllib.request.Request(url, headers=_qq_headers())
    try:
        with _safe_urlopen(req, timeout=4) as r:
            data = json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))
            raw = data.get('lyric', '')
        lines = []
        for line in raw.splitlines():
            s = html.unescape(line.strip())
            tags = re.findall(r'\[(\d+):(\d+(?:\.\d+)?)\]', s)
            if not tags:
                continue
            txt = re.sub(r'^(?:\[\d+:\d+(?:\.\d+)?\])+', '', s).strip()
            if txt and not txt.startswith('offset:'):
                for mm, ss in tags:
                    lines.append({'time': round(int(mm) * 60 + float(ss), 2), 'text': txt})
        lines.sort(key=lambda x: x['time'])
        # 2026-11-07 Claude: cache only real answers. An error reply (QQ fails now and then; a bad mid
        # gives retcode 1101) has no lyric and must not pin "no lyrics" on the song until a restart.
        if lines or data.get('retcode', data.get('code')) == 0:
            with _LYRIC_LOCK:
                if len(_LYRIC_CACHE) >= 64:
                    _LYRIC_CACHE.pop(next(iter(_LYRIC_CACHE)), None)
                _LYRIC_CACHE[mid] = list(lines)
        return lines
    except Exception as e:
        log.warning('qq_lyric error: %s', e)
        return []


def _ambient_file_for(preset: str):
    for ext in AMBIENT_EXTS:  # 2026-11-12: the seamless FLAC loop when present, else the old AAC one
        try:
            p = AMBIENT_DIR / f'{preset}{ext}'
            if p.exists() and not p.is_symlink() and p.is_file() and p.stat().st_size > 16384:
                return p
        except OSError:
            pass
    return None


def _fallback_ambient_pcm(preset: str) -> bytes:
    """Offline fallback 4.0s 48kHz stereo float32 loop when cached .m4a is absent (e.g. unit tests)."""
    n = OUT_RATE * 4
    if _np is None:
        buf = _array('f', [0.0] * (n * 2))
        for i in range(0, min(n, 4800)):
            v = 0.04 * math.sin(2.0 * math.pi * 220.0 * i / OUT_RATE)
            buf[2 * i] = v
            buf[2 * i + 1] = v
        return buf.tobytes()
    rng = _np.random.default_rng({
        'rain': 101, 'waves': 303, 'forest': 404, 'stream': 455, 'night': 505, 'sleep_piano': 606,
        'fireplace': 717, 'library': 828, 'singing_bowl': 939,
    }.get(preset, 707))
    freqs = _np.fft.rfftfreq(n, d=1.0 / OUT_RATE)
    f_safe = _np.maximum(freqs, 0.25)
    env = (f_safe ** (-0.65)).astype(_np.float32) / (1.0 + (120.0 / f_safe) ** 3) / (1.0 + (f_safe / 3600.0) ** 3)
    env[0] = 0.0
    ph_l = rng.uniform(0.0, 2.0 * math.pi, size=freqs.shape[0])
    ph_r = rng.uniform(0.0, 2.0 * math.pi, size=freqs.shape[0])
    ch_l = _np.fft.irfft(env * (_np.cos(ph_l) + 1j * _np.sin(ph_l)), n=n).astype(_np.float32)
    ch_r = _np.fft.irfft(env * (_np.cos(ph_r) + 1j * _np.sin(ph_r)), n=n).astype(_np.float32)
    ch_l = (ch_l / max(1e-6, float(_np.std(ch_l)))) * 0.08
    ch_r = (ch_r / max(1e-6, float(_np.std(ch_r)))) * 0.08
    stereo = _np.empty((n, 2), dtype=_np.float32)
    stereo[:, 0] = _np.clip(ch_l, -0.7, 0.7)
    stereo[:, 1] = _np.clip(ch_r, -0.7, 0.7)
    return stereo.tobytes()


def qq_user_playlists() -> list:
    cfg = load_config()
    uin = str(cfg.get('uin') or '').strip()
    if not uin or not uin.isdigit():
        return []
    fav_out = []
    created_out = []
    created_lock = threading.Lock()

    def _fetch_created(use_cookies: bool = True):
        # 2. Fetch user-created playlists via fcg_user_created_diss
        try:
            url2 = f'https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss?hostuin={uin}&sin=0&size=40&r={int(time.time())}&format=json&utf8=1'
            hdrs = _qq_headers() if use_cookies else {'User-Agent': UA}
            hdrs['Referer'] = 'https://y.qq.com/portal/profile.html'
            req2 = urllib.request.Request(url2, headers=hdrs)
            with _safe_urlopen(req2, timeout=5) as r2:
                j2 = json.loads(_read_http_bytes(r2).decode('utf-8', errors='replace'))
                d2 = j2.get('data') or {}
                batch = []
                for item in d2.get('disslist') or []:
                    tid = item.get('tid')
                    cnt = int(item.get('song_cnt') or 0)
                    name = (item.get('diss_name') or '').strip()
                    if not tid or cnt <= 0 or name in ('我喜欢', '默认收藏', 'QZone背景音乐'):
                        continue
                    batch.append({
                        'id': str(tid),
                        'dissid': str(tid),
                        'name': f'🎵 {name}',
                        'count': cnt,
                        'cover': item.get('diss_cover') or '',
                    })
                if batch:
                    with created_lock:
                        if not created_out:
                            created_out.extend(batch)
        except Exception as e:
            log.warning('qq_user_playlists created_diss error: %s', e)

    t_created = threading.Thread(target=_fetch_created, daemon=True)
    t_created.start()

    # 1. Fetch "我喜欢" (dirid=201) via music.srfDissInfo.aiDissInfo
    global _DIR201_CACHE
    refreshed_ck = False
    try:
        has_ck = bool((cfg.get('cookies_str') or cfg.get('qm_keyst') or '').strip())
        req_body = {
            'module': 'music.srfDissInfo.aiDissInfo',
            'method': 'uniform_get_Dissinfo',
            'param': {'dirid': 201, 'hostUin': int(uin), 'sin': 0, 'size': 160, 'orderlist': 1},
        }

        def _req_dir201(use_ck: bool):
            comm = _qq_comm() if use_ck else {'ct': 24, 'cv': 0, 'uin': uin, 'format': 'json'}
            hdrs = _qq_headers() if use_ck else {'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'}
            req1 = urllib.request.Request(
                'https://u.y.qq.com/cgi-bin/musicu.fcg',
                data=json.dumps({'comm': comm, 'req': req_body}).encode('utf-8'),
                headers=hdrs,
            )
            with _safe_urlopen(req1, timeout=5) as r1:
                j1 = json.loads(_read_http_bytes(r1).decode('utf-8', errors='replace'))
            rn = j1.get('req') or {}
            rc = rn.get('code') if 'code' in rn else j1.get('code', 0)
            return rc, (rn.get('data') or {})

        r_code, d1 = _req_dir201(has_ck)
        if has_ck and r_code in (1000, 2001) and _refresh_musickey_from_cookies():
            refreshed_ck = True
            cfg = load_config()
            r_code, d1 = _req_dir201(True)
        if has_ck and r_code != 0:
            r_code, d1 = _req_dir201(False)

        if r_code == 0 and d1:
            dirinfo = d1.get('dirinfo') or {}
            songnum = dirinfo.get('songnum') or d1.get('total_song_num') or 0
            diss_id = dirinfo.get('id') or 'dir_201'
            dq = _default_quality_label(cfg)
            songs = d1.get('songlist') or []
            with _DIR201_LOCK:
                _DIR201_CACHE = (uin, dq, time.monotonic(), [_format_qq_song(s, default_q=dq) for s in songs[:160] if s.get('mid')])
            fav_out.append({
                'id': 'dir_201',
                'dissid': str(diss_id),
                'name': '❤️ 我喜欢',
                'count': int(songnum),
                'cover': dirinfo.get('picurl') or 'https://y.gtimg.cn/mediastyle/y/img/cover_love_300.jpg',
            })
    except Exception as e:
        log.warning('Failed to fetch dirid=201: %s', e)

    t_created.join(timeout=6.0)
    with created_lock:
        got_created = bool(created_out)
    if not got_created and refreshed_ck:
        _fetch_created(True)
    with created_lock:
        return fav_out + list(created_out)


# ---- 2026-11-12 (Claude): 我喜欢 (dirid 201) for the playing song ----------------------------------------------------
_SONG_ID_CACHE = {}   # mid -> (numeric song id, song type); playlist writes take the numeric id


def _qq_client_comm(cfg: dict = None) -> dict:
    """The comm the QQ Music client sends: playlist writes refuse the web-style one (code 80105)."""
    cfg = cfg or load_config()
    return {'ct': '11', 'cv': '13020508', 'v': '13020508', 'tmeAppID': 'qqmusic', 'format': 'json',
            'inCharset': 'utf-8', 'outCharset': 'utf-8', 'qq': str(cfg.get('uin') or ''),
            'authst': str(cfg.get('qm_keyst') or ''), 'tmeLoginType': '1' if _is_wx_login(cfg) else '2'}


def _qq_musicu_one(comm: dict, req: dict, timeout: float = 8) -> tuple:
    """One musicu.fcg call: (code, data)."""
    body = json.dumps({'comm': comm, 'req_1': req}).encode('utf-8')
    r = urllib.request.Request('https://u.y.qq.com/cgi-bin/musicu.fcg', data=body, headers=_qq_headers())
    with _safe_urlopen(r, timeout=timeout) as resp:
        j = json.loads(_read_http_bytes(resp).decode('utf-8', errors='replace'))
    rq = j.get('req_1') if isinstance(j.get('req_1'), dict) else {}
    return rq.get('code', j.get('code')), (rq.get('data') if isinstance(rq.get('data'), dict) else {})


def qq_song_id(mid: str) -> tuple:
    """(numeric id, type) of a QQ song, or (None, None)."""
    hit = _SONG_ID_CACHE.get(mid)
    if hit:
        return hit
    try:
        code, d = _qq_musicu_one({'ct': 24, 'cv': 0, 'format': 'json'},
                                 {'module': 'music.pf_song_detail_svr', 'method': 'get_song_detail_yqq', 'param': {'song_mid': mid}})
    except Exception as e:   # noqa: BLE001
        log.warning('song id lookup failed: %s', type(e).__name__)
        return None, None
    ti = d.get('track_info') if isinstance(d.get('track_info'), dict) else {}
    sid, stype = ti.get('id'), ti.get('type')
    if code != 0 or type(sid) is not int or sid <= 0:
        return None, None
    res = (sid, stype if type(stype) is int else 0)
    if len(_SONG_ID_CACHE) > 500:
        _SONG_ID_CACHE.clear()
    _SONG_ID_CACHE[mid] = res
    return res


def qq_fav_state(mid: str):
    """True / False: is the song in 我喜欢; None when it cannot be told (no login, network)."""
    cfg = load_config()
    if not cfg.get('uin') or not cfg.get('qm_keyst'):
        return None
    req = {'module': 'music.musicasset.SongFavRead', 'method': 'IsSongFanByMid', 'param': {'v_songMid': [mid]}}
    try:
        code, d = _qq_musicu_one(_qq_client_comm(cfg), req, 6)
        if code in (1000, 2001) and _refresh_musickey_from_cookies():
            code, d = _qq_musicu_one(_qq_client_comm(), req, 6)
    except Exception as e:   # noqa: BLE001
        log.warning('我喜欢 state failed: %s', type(e).__name__)
        return None
    fan = d.get('m_fan') if isinstance(d.get('m_fan'), dict) else {}
    return bool(fan.get(mid)) if code == 0 and mid in fan else None


def qq_fav_set(mid: str, on: bool) -> dict:
    """Add the song to 我喜欢 (on) or take it out."""
    global _DIR201_CACHE
    cfg = load_config()
    if not cfg.get('uin') or not cfg.get('qm_keyst'):
        return {'ok': False, 'login': False, 'error': '还没有登录 QQ 音乐，请在「账号信息」里扫码登录'}
    sid, stype = qq_song_id(mid)
    if not sid:
        return {'ok': False, 'error': '这首歌查不到编号，没法收藏'}
    req = {'module': 'music.musicasset.PlaylistDetailWrite', 'method': 'AddSonglist' if on else 'DelSonglist',
           'param': {'dirId': 201, 'v_songInfo': [{'songType': stype, 'songId': sid}]}}
    try:
        code, _ = _qq_musicu_one(_qq_client_comm(cfg), req)
        if code in (1000, 2001) and _refresh_musickey_from_cookies():
            code, _ = _qq_musicu_one(_qq_client_comm(), req)
    except Exception as e:   # noqa: BLE001
        log.warning('我喜欢 write failed: %s', type(e).__name__)
        return {'ok': False, 'error': '连不上 QQ 音乐，请稍后再试'}
    if code in (1000, 2001):
        return {'ok': False, 'login': False, 'error': 'QQ 音乐登录过期了，请在「账号信息」里重新扫码登录'}
    if code != 0:
        log.warning('我喜欢 write refused: code %s', code)
        return {'ok': False, 'error': f'QQ 音乐没有接受（{code}），请稍后再试'}
    with _DIR201_LOCK:
        _DIR201_CACHE = None
    log.info('我喜欢: %s %s', 'added' if on else 'removed', mid)
    return {'ok': True, 'mid': mid, 'liked': bool(on), 'message': '已加入「我喜欢」' if on else '已从「我喜欢」移除'}


def qq_playlist_tracks(dissid: str, limit: int = 160) -> list:
    global _DIR201_CACHE
    cfg = load_config()
    uin = str(cfg.get('uin') or '0').strip()
    dissid_str = str(dissid or '').strip()
    has_ck = bool((cfg.get('cookies_str') or cfg.get('qm_keyst') or '').strip())

    # 1. If "我喜欢" (dir_201), fetch via uniform_get_Dissinfo dirid=201
    if dissid_str == 'dir_201' and uin.isdigit():
        dq = _default_quality_label(cfg)
        with _DIR201_LOCK:
            c201 = _DIR201_CACHE
        if c201 and c201[0] == uin and c201[1] == dq and (time.monotonic() - c201[2]) < 20.0 and limit <= 160:
            return list(c201[3][:limit])
        try:
            req_body = {
                'module': 'music.srfDissInfo.aiDissInfo',
                'method': 'uniform_get_Dissinfo',
                'param': {'dirid': 201, 'hostUin': int(uin), 'sin': 0, 'size': limit, 'orderlist': 1},
            }

            def _req_tracks201(use_ck: bool):
                comm = _qq_comm() if use_ck else {'ct': 24, 'cv': 0, 'uin': uin, 'format': 'json'}
                hdrs = _qq_headers() if use_ck else {'User-Agent': UA, 'Referer': 'https://y.qq.com/', 'Content-Type': 'application/json'}
                req1 = urllib.request.Request(
                    'https://u.y.qq.com/cgi-bin/musicu.fcg',
                    data=json.dumps({'comm': comm, 'req': req_body}).encode('utf-8'),
                    headers=hdrs,
                )
                with _safe_urlopen(req1, timeout=6) as r1:
                    j1 = json.loads(_read_http_bytes(r1).decode('utf-8', errors='replace'))
                rn = j1.get('req') or {}
                rc = rn.get('code') if 'code' in rn else j1.get('code', 0)
                return rc, (rn.get('data') or {})

            r_code, d1 = _req_tracks201(has_ck)
            if has_ck and r_code in (1000, 2001) and _refresh_musickey_from_cookies():
                cfg = load_config()
                r_code, d1 = _req_tracks201(True)
            if has_ck and r_code != 0:
                r_code, d1 = _req_tracks201(False)
            if r_code == 0 and d1:
                songs = d1.get('songlist') or []
                dq = _default_quality_label(cfg)
                out = [_format_qq_song(s, default_q=dq) for s in songs[:limit] if s.get('mid')]
                if limit >= 160:
                    with _DIR201_LOCK:
                        _DIR201_CACHE = (uin, dq, time.monotonic(), list(out[:160]))
                return out
            return []
        except Exception as e:
            log.warning('qq_playlist_tracks dir_201 error: %s', e)
            return []

    # 2. Normal playlist dissid
    if not dissid_str:
        return []
    q_dissid = urllib.parse.quote(dissid_str, safe='')
    q_uin = urllib.parse.quote(uin if (uin.isascii() and uin.isdigit()) else '0', safe='')
    url = (
        f'https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg'
        f'?type=1&json=1&utf8=1&onlysong=0&new_format=1&disstid={q_dissid}&loginUin={q_uin}&format=json'
    )

    def _req_diss(use_ck: bool):
        hdrs = _qq_headers() if use_ck else {'User-Agent': UA, 'Referer': 'https://y.qq.com/'}
        with _safe_urlopen(urllib.request.Request(url, headers=hdrs), timeout=6) as r:
            return json.loads(_read_http_bytes(r).decode('utf-8', errors='replace'))

    try:
        data = _req_diss(has_ck)
        if has_ck and data.get('code') in (1000, 2001) and _refresh_musickey_from_cookies():
            cfg = load_config()
            data = _req_diss(True)
        if has_ck and not (data.get('cdlist') or []):
            data = _req_diss(False)
        cdlist = data.get('cdlist') or []
        if cdlist:
            songs = cdlist[0].get('songlist') or []
            dq = _default_quality_label(cfg)
            return [_format_qq_song(s, default_q=dq) for s in songs[:limit] if s.get('mid')]
    except Exception as e:
        log.warning('qq_playlist_tracks error: %s', e)
    return []


def search_local(query: str = '') -> list:
    # /home/ubuntu/Music/usb points back to /mnt/usb. Follow directory links only inside
    # allowed LOCAL_DIRS roots, visit each inode once, and bound work before returning to the UI.
    q = (query or '').strip().lower()
    allowed_roots = []
    for d in LOCAL_DIRS:
        try:
            if d.exists():
                allowed_roots.append(d.resolve())
        except OSError:
            continue
    if not allowed_roots:
        return []
    out = []
    seen_dirs = set()
    seen_files = set()
    pending = list(reversed(LOCAL_DIRS))
    entries = 0
    while pending and len(seen_dirs) < 1024 and entries < 10000:
        directory = Path(pending.pop())
        try:
            dir_rp = directory.resolve()
            if not any(dir_rp.is_relative_to(rt) for rt in allowed_roots):
                continue
            st = directory.stat()
            identity = (st.st_dev, st.st_ino)
            if identity in seen_dirs:
                continue
            seen_dirs.add(identity)
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > 10000:
                        break
                    try:
                        if child.is_dir(follow_symlinks=True):
                            child_rp = Path(child.path).resolve()
                            if any(child_rp.is_relative_to(rt) for rt in allowed_roots):
                                pending.append(Path(child.path))
                            continue
                        p = Path(child.path)
                        if p.suffix.lower() not in AUDIO_EXTS or (q and q not in p.stem.lower()):
                            continue
                        if not child.is_file(follow_symlinks=True):
                            continue
                        rp = p.resolve()
                        if not rp.is_file() or not any(rp.is_relative_to(rt) for rt in allowed_roots):
                            continue
                        st = child.stat(follow_symlinks=True)
                        identity = (st.st_dev, st.st_ino)
                        if identity in seen_files:
                            continue
                        seen_files.add(identity)
                        out.append({
                            'mid': '',
                            'title': p.stem,
                            'artist': 'U盘/本地音乐',
                            'album': p.parent.name,
                            'cover': '',
                            'duration': 0,
                            'source': 'U盘无损/本地' if '/mnt/usb' in str(rp) else '本地音乐',
                            'quality': p.suffix.upper().lstrip('.'),
                            'url': str(rp),
                            'headers': {},
                            'is_local': True,
                        })
                        if len(out) >= 50:
                            return out
                    except OSError:
                        continue
        except OSError:
            continue
    return out


class QQMusicEngine:
    """Standalone 48kHz Stereo Player with Progress Tracking, Lyrics, and Voice Ducking."""
    ambient_preset = None
    ambient_layer = None
    ambient_layer_gain = 0.45
    _ambient_buf = None
    _ambient_layer_buf = None
    _ambient_worker_gen = -1
    _proc2 = None

    def __init__(self):
        self._lock = threading.Lock()
        self._vol_lock = threading.Lock()
        self.queue = []
        self.index = 0
        self.playing = False
        self.paused = False
        self.ducked = False
        self.alarm_ringing = False
        self.bt_streaming = False
        self._bt_pause_sent_mono = 0.0
        self._local_vol_mono = 0.0
        self._just_started_mono = None
        self.boot_id = secrets.randbits(48) or 1
        self.open_ui_seq = 0
        self._open_ui_mono = 0.0
        self.queue_seq = 1
        self._cached_q_seq = 0
        self._cached_queue_pub = []
        self.lyrics_seq = 1
        self.lyrics = []
        self.elapsed_base = 0.0
        self.play_started_mono = 0.0
        self.volume = 55
        self._proc = None
        self._proc2 = None
        self._pacat = None
        self._gen = 0
        self._starting_gen = -1
        self._fail_streak = 0
        self._shutting_down = False
        self.aec_clients = set()
        self._aec_lock = threading.Lock()
        # Round 109 sleep timer: 'time' pauses at sleep_deadline (monotonic), 'tracks' pauses after
        # sleep_tracks_left tracks have ended naturally; the last SLEEP_FADE_SECONDS fade out smoothly.
        self.sleep_mode = None
        self.sleep_deadline = 0.0
        self.sleep_total = 0
        self.sleep_tracks_left = 0
        # 2026-11-12: fade length per timer, all-night mode, sleep sessions (sound + timer + own volume)
        self.sleep_fade = self.SLEEP_FADE_SECONDS
        self.sleep_night = False
        self.sleep_started_mono = 0.0
        self.sleep_ends_wall = None
        self._sg_last = (1.0, 0.0)
        self.sleep_session = None
        self._sleep_idle_since = None
        self._sleep_state = _read_sleep_state()
        self.ambient_preset = None
        self.ambient_layer = None
        self.ambient_layer_gain = 0.45
        self.ambient_seq = 0
        self._ambient_buf = None
        self._ambient_layer_buf = None
        self._ambient_worker_gen = -1
        self.study_duck_until = 0.0
        self._duck_leases = {}
        init_mode = load_config().get('play_mode', 'loop')
        self.play_mode = init_mode if init_mode in ('loop', 'single', 'shuffle') else 'loop'
        self._read_initial_volume()
        self._load_session()

    def _clear_duck_locked(self):
        self.study_duck_until = 0.0
        leases = getattr(self, '_duck_leases', None)
        if isinstance(leases, dict):
            leases.clear()

    def _clear_duck_except_home_locked(self):
        now_m = time.monotonic()
        leases = getattr(self, '_duck_leases', None)
        home_until = 0.0
        if isinstance(leases, dict):
            raw_h = leases.get('home', 0.0)
            if isinstance(raw_h, (int, float)) and raw_h > now_m:
                home_until = float(raw_h)
            leases.clear()
            if home_until > 0.0:
                leases['home'] = home_until
        self.study_duck_until = home_until

    @staticmethod
    def _safe_dur(val) -> int:
        if isinstance(val, bool) or val is None:
            return 0
        try:
            f = float(val)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(f) or f <= 0:
            return 0
        return min(86400, int(round(f)))

    @classmethod
    def _public_track(cls, t: dict):
        if not isinstance(t, dict):
            return None
        dur = cls._safe_dur(t.get('duration'))
        return {
            'mid': str(t.get('mid') or ''),
            'media_mid': str(t.get('media_mid') or ''),
            'file': str(t.get('url') or '') if t.get('is_local') else '',
            'title': str(t.get('title') or ''),
            'artist': str(t.get('artist') or ''),
            'album': str(t.get('album') or ''),
            'cover': str(t.get('cover') or ''),
            'thumb': str(t.get('thumb') or t.get('cover') or ''),
            'source': str(t.get('source') or 'QQ音乐'),
            'quality': str(t.get('quality') or ''),
            'duration': dur,
            'vip': bool(t.get('vip')),
            'is_local': bool(t.get('is_local')),
        }

    def _load_session(self):
        try:
            if not SESSION_PATH.exists() or SESSION_PATH.is_symlink():
                return
            data = json.loads(_read_regular_text(SESSION_PATH, private=True))
            if not isinstance(data, dict):
                return
            q = data.get('queue')
            if data.get('playing') and isinstance(q, list) and q:
                cleaned_q = []
                for x in q[:200]:
                    if not isinstance(x, dict):
                        continue
                    d = dict(x)
                    if not d.get('is_local'):
                        d['url'] = None
                    cleaned_q.append(d)
                self.queue = cleaned_q
                if self.queue:
                    raw_idx = data.get('index')
                    try:
                        idx_val = 0 if isinstance(raw_idx, bool) or raw_idx is None else int(raw_idx)
                    except (TypeError, ValueError, OverflowError):
                        idx_val = 0
                    self.index = max(0, min(idx_val, len(self.queue) - 1))
                    raw_eb = data.get('elapsed_base')
                    try:
                        eb_val = 0.0 if isinstance(raw_eb, bool) or raw_eb is None else float(raw_eb)
                    except (TypeError, ValueError, OverflowError):
                        eb_val = 0.0
                    self.elapsed_base = eb_val if math.isfinite(eb_val) and eb_val >= 0.0 else 0.0
                    lyr = data.get('lyrics')
                    self.lyrics = [x for x in lyr[:300] if isinstance(x, dict)] if isinstance(lyr, list) else []
                    self.playing = True
                    self.paused = True
        except Exception as e:
            log.warning('Failed to load session: %s', e)

    def save_session(self):
        # Serialize the snapshot with its write: an older background save must not
        # replace a newer user action or share its temporary file.
        with _SESSION_LOCK:
            self._save_session_locked()

    def _save_session_locked(self):
        try:
            with self._lock:
                if getattr(self, '_shutting_down', False):
                    return
                playing = bool(self.playing and self.queue)
                data = {
                    'playing': playing,
                    'index': self.index if playing else 0,
                    'elapsed_base': round(self.elapsed_base, 1) if playing else 0.0,
                    'lyrics': list(self.lyrics[:300]) if playing else [],
                    'queue': [
                        {
                            'mid': x.get('mid', ''),
                            'media_mid': x.get('media_mid', ''),
                            'title': x.get('title', ''),
                            'artist': x.get('artist', ''),
                            'album': x.get('album', ''),
                            'cover': x.get('cover', ''),
                            'thumb': x.get('thumb') or x.get('cover', ''),
                            'duration': x.get('duration', 0),
                            'vip': bool(x.get('vip')),
                            'source': x.get('source', ''),
                            'quality': x.get('quality', ''),
                            'url': x.get('url') if x.get('is_local') else None,
                            'is_local': bool(x.get('is_local')),
                        }
                        for x in (self.queue[:200] if playing else [])
                    ],
                }
            SESSION_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                pst = os.lstat(SESSION_PATH.parent)
                if stat.S_ISDIR(pst.st_mode) and pst.st_uid == os.getuid():
                    os.chmod(SESSION_PATH.parent, 0o700)
            tmp = SESSION_PATH.parent / f'.{SESSION_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp'
            created = False
            try:
                if SESSION_PATH.is_symlink():
                    raise OSError('Refusing symlink session path')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                created = True
                with os.fdopen(fd, 'w', encoding='utf-8') as tf:
                    os.fchmod(tf.fileno(), 0o600)
                    tf.write(json.dumps(data, ensure_ascii=False))
                    tf.flush()
                    st0 = os.fstat(tf.fileno())
                st1 = os.lstat(tmp)
                if not stat.S_ISREG(st1.st_mode) or (st1.st_dev, st1.st_ino) != (st0.st_dev, st0.st_ino) or SESSION_PATH.is_symlink():
                    raise OSError('Session temp file changed unexpectedly')
                os.replace(tmp, SESSION_PATH)
            finally:
                if created:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception as e:
            log.warning('Failed to save session: %s', e)

    def set_play_mode(self, mode: str) -> str:
        if mode not in ('loop', 'single', 'shuffle'):
            mode = 'loop'
        with self._lock:
            self.play_mode = mode
        with _CFG_LOCK:
            cfg = load_config()
            if cfg.get('play_mode') != mode:
                cfg['play_mode'] = mode
                save_config(cfg)
        return mode

    def _read_initial_volume(self):
        try:
            if _RING_VOL_PATH.exists() and not _RING_VOL_PATH.is_symlink():
                rv = json.loads(_read_regular_text(_RING_VOL_PATH, 65536)).get('volume')
                if isinstance(rv, (int, float)) and not isinstance(rv, bool) and math.isfinite(rv):
                    self.volume = max(0, min(100, int(round(rv))))
                    return
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'],
                env={**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000'},
                text=True,
                timeout=2,
            )
            m = re.search(r'([\d.]+)', out)
            if m:
                self.volume = int(round(float(m.group(1)) * 100))
        except Exception:
            pass

    def _pause_bluetooth_if_streaming(self):
        with self._lock:
            if not self.bt_streaming:
                return
            self._bt_pause_sent_mono = time.monotonic()

        def _worker():
            try:
                subprocess.run(
                    ['/usr/bin/python3', '-c', _BT_PAUSE_CODE],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    @property
    def just_started(self) -> bool:
        return self._just_started_mono is not None and (time.monotonic() - self._just_started_mono) < 15.0

    @just_started.setter
    def just_started(self, val: bool):
        self._just_started_mono = time.monotonic() if (val and self.ducked) else None

    def set_volume(self, vol: int = None, delta: int = None) -> int:
        if (vol is None and delta is None) or isinstance(vol, bool) or isinstance(delta, bool):
            return self.volume
        with self._lock:
            try:
                if delta is not None:
                    self.volume = max(0, min(100, self.volume + int(float(delta))))
                elif vol is not None:
                    self.volume = max(0, min(100, int(float(vol))))
            except (TypeError, ValueError, OverflowError):
                return self.volume
            self._local_vol_mono = time.monotonic()
            new_vol = self.volume
        with self._vol_lock:
            with self._lock:
                new_vol = self.volume
            synced = False
            try:
                req = urllib.request.Request(
                    'http://127.0.0.1:8090/api/volume',
                    data=json.dumps({'volume': new_vol}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with _LOCAL_OPENER.open(req, timeout=1.5) as r:
                    res = json.loads(r.read(256).decode('utf-8', errors='replace'))
                    synced = r.status == 200 and isinstance(res, dict) and res.get('ok') is True
            except Exception:
                synced = False
            if not synced:
                try:
                    subprocess.run(
                        ['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{new_vol / 100.0:.2f}'],
                        env={**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000'},
                        timeout=2,
                    )
                except Exception:
                    pass
        return new_vol

    def get_elapsed(self) -> float:
        with self._lock:
            if not self.playing:
                return 0.0
            return self.elapsed_base

    _AEC_FLUSH_PKT = b'QQM_FLUSH_BUFFER' * 480  # 7680 bytes

    def _send_aec_flush(self):
        with self._aec_lock:
            for sock in list(self.aec_clients):
                try:
                    sock.sendall(self._AEC_FLUSH_PKT)
                except Exception:
                    self.aec_clients.discard(sock)
                    try:
                        sock.close()
                    except Exception:
                        pass

    def set_ducked(self, ducked: bool):
        do_flush = False
        with self._lock:
            if self.ducked == bool(ducked):
                return
            self.ducked = bool(ducked)
            if self.ducked:
                do_flush = True
            else:
                self._just_started_mono = None
        if do_flush:
            self._send_aec_flush()

    SLEEP_FADE_SECONDS = 20.0

    def _clear_sleep_locked(self):
        self.sleep_mode = None
        self.sleep_deadline = 0.0
        self.sleep_total = 0
        self.sleep_tracks_left = 0
        self.sleep_fade = self.SLEEP_FADE_SECONDS
        self.sleep_night = False
        self.sleep_ends_wall = None
        self._sg_last = (1.0, time.monotonic())

    def _sleep_track_left_locked(self):
        t = self.queue[self.index] if (self.playing and 0 <= self.index < len(self.queue)) else None
        dur = self._safe_dur(t.get('duration')) if t else 0
        return dur, (max(0.0, dur - self.elapsed_base) if dur else 0.0)

    def _sleep_public_locked(self):
        if self.sleep_mode == 'time':
            if getattr(self, '_preview', None) is not None:
                return None  # a preview's own short timer is not a sleep timer for the pages
            remaining = max(0.0, self.sleep_deadline - time.monotonic())
            fade = float(getattr(self, 'sleep_fade', self.SLEEP_FADE_SECONDS) or self.SLEEP_FADE_SECONDS)
            ends = getattr(self, 'sleep_ends_wall', None)
            return {'mode': 'time', 'total': self.sleep_total, 'remaining': int(math.ceil(remaining)),
                    'fading': remaining <= fade, 'fade': int(fade), 'night': bool(getattr(self, 'sleep_night', False)),
                    'ends_at': int(ends) if ends else None}
        if self.sleep_mode == 'tracks':
            dur, left = self._sleep_track_left_locked()
            last = self.sleep_tracks_left == 1
            return {'mode': 'tracks', 'total': self.sleep_total, 'tracks_left': self.sleep_tracks_left,
                    'remaining': int(math.ceil(left)) if (last and dur) else None,
                    'fading': bool(last and dur > 2 * self.SLEEP_FADE_SECONDS and left <= self.SLEEP_FADE_SECONDS)}
        return None

    def sleep_status(self):
        with self._lock:
            return self._sleep_public_locked()

    def set_sleep(self, minutes=None, tracks=None, cancel=False, fade=None, until=None, night=False):
        """minutes 1-240 | tracks 1-50 | until 'HH:MM' | night (until the morning, quieter after 30 minutes).
        fade: seconds of fade-out at the end (default 20 s; sleep sessions 600 s), at most a third of the timer."""
        def _int(v, lo, hi):
            if v is None or isinstance(v, bool):
                return None
            try:
                n = int(float(v))
            except (TypeError, ValueError, OverflowError):
                return None
            return n if lo <= n <= hi else None

        fd = None
        if fade is not None and not isinstance(fade, bool):
            try:
                fd = float(fade)
            except (TypeError, ValueError, OverflowError):
                fd = None
            fd = fd if (fd is not None and math.isfinite(fd) and 3.0 <= fd <= 1800.0) else None
        secs = None
        if night is True:
            secs = _seconds_until('%02d:%02d' % SLEEP_MORNING)
            if secs is None or secs > 12 * 3600:  # "all night" in the day: three hours
                secs = 3 * 3600.0
        elif until is not None and minutes is None and tracks is None:
            secs = _seconds_until(until)
            if secs is None or secs > 12 * 3600:
                return {'ok': False, 'error': '到点时间要写成 HH:MM，且在 12 小时以内'}
        cancel_sess = False
        with self._lock:
            if cancel:
                had = self.sleep_mode is not None
                cancel_sess = getattr(self, 'sleep_session', None) is not None
                self._clear_sleep_locked()
            else:
                had = False
        if cancel:
            if cancel_sess:
                self._sleep_session_end('cancel')
            return {'ok': True, 'sleep': None, 'message': '已取消定时关闭' if had else '没有设置定时关闭'}
        with self._lock:
            m, n = _int(minutes, 1, 240), _int(tracks, 1, 50)
            if secs is None and m is None and n is not None and getattr(self, 'ambient_preset', None):
                m = max(10, min(240, n * 15))
                n = None
            if secs is None and m is not None:
                secs = m * 60.0
            if secs is not None:
                if night is True:
                    fade_s = 180.0
                else:
                    fade_s = fd if fd is not None else self.SLEEP_FADE_SECONDS
                    fade_s = min(fade_s, max(self.SLEEP_FADE_SECONDS, secs / 3.0))
                self.sleep_mode = 'time'
                self.sleep_deadline = time.monotonic() + secs
                self.sleep_total = max(1, int(math.ceil(secs / 60.0)))
                self.sleep_tracks_left = 0
                self.sleep_fade = fade_s
                self.sleep_night = night is True
                self.sleep_started_mono = time.monotonic()
                self.sleep_ends_wall = time.time() + secs
                end_txt = time.strftime('%H:%M', time.localtime(self.sleep_ends_wall))
                if night is True:
                    msg = f'好的，整夜播放：半小时后自动调小，早上 {end_txt} 停止'
                elif until is not None and minutes is None:
                    msg = f'好的，{end_txt} 停止' + (f'，最后 {int(round(fade_s / 60))} 分钟慢慢变小' if fade_s >= 60 else '，到点前慢慢变小')
                elif fade_s >= 60:
                    msg = f'好的，{self.sleep_total} 分钟后停止，最后 {int(round(fade_s / 60))} 分钟慢慢变小'
                else:
                    msg = f'好的，{self.sleep_total} 分钟后音乐会慢慢变小并停止'
            elif n is not None:
                self.sleep_mode = 'tracks'
                self.sleep_deadline = 0.0
                self.sleep_total = n
                self.sleep_tracks_left = n
                self.sleep_fade = self.SLEEP_FADE_SECONDS
                self.sleep_night = False
                self.sleep_ends_wall = None
                msg = '好的，播完这首就停' if n == 1 else f'好的，再播 {n} 首就停'
            else:
                return {'ok': False, 'error': '定时时长需在 1-240 分钟，或 1-50 首'}
            pub = self._sleep_public_locked()
        log.info('Sleep timer set: %s', pub)
        return {'ok': True, 'sleep': pub, 'message': msg}

    def _sleep_gain_locked(self) -> float:
        """Gain (0..1) for the PCM block about to be played; 1.0 when no fade is due.
        Short fades (<= 60 s) use x*x; long ones fall in even loudness steps to -45 dB. All night: after
        SLEEP_NIGHT_SETTLE the sound goes down by SLEEP_NIGHT_DB. The gain never jumps up: at most +6 dB a second."""
        now = time.monotonic()
        g = 1.0
        if self.sleep_mode == 'time':
            rem = self.sleep_deadline - now
            fade = float(getattr(self, 'sleep_fade', self.SLEEP_FADE_SECONDS) or self.SLEEP_FADE_SECONDS)
            if rem < fade:
                x = max(0.0, rem / fade)
                g = x * x if fade <= 60.0 else (0.0 if rem <= 0 else 10 ** (-2.25 * (1.0 - x)))
            if getattr(self, 'sleep_night', False):
                k = (now - getattr(self, 'sleep_started_mono', now) - SLEEP_NIGHT_SETTLE) / SLEEP_NIGHT_RAMP
                if k > 0:
                    g *= 10 ** (SLEEP_NIGHT_DB / 20.0 * min(1.0, k))
        elif self.sleep_mode == 'tracks' and self.sleep_tracks_left == 1 and not getattr(self, 'ambient_preset', None):
            dur, rem = self._sleep_track_left_locked()
            if dur > 2 * self.SLEEP_FADE_SECONDS and rem < self.SLEEP_FADE_SECONDS:
                x = max(0.0, rem / self.SLEEP_FADE_SECONDS)
                g = x * x
        wk = getattr(self, '_wake', None)
        if wk is not None and getattr(self, 'ambient_preset', None) == wk['preset'] and not (self.playing and not self.paused):
            t = now - wk['start']
            if t < 3.0:
                g *= (max(0.0, t) / 3.0) ** 2   # a soft start; the volume (20% -> 50%) does the rest
            left = wk['end'] - now
            fade = float(wk.get('fade') or SLEEP_WAKE_FADE)
            if left < fade:
                g *= max(0.0, left / fade) ** 2
        last_g, last_t = getattr(self, '_sg_last', (1.0, 0.0))
        if g > last_g + 1e-4:
            g = min(g, max(last_g, 0.003) * 10 ** (0.3 * min(1.0, max(0.0, now - last_t))))
        self._sg_last = (g, now)
        return g

    @staticmethod
    def _apply_gain(buf: bytearray, g0: float, g1: float) -> None:
        """Scale one block of interleaved stereo float32 PCM in place, ramping g0 -> g1 (no zipper noise)."""
        frames = len(buf) // 8
        if frames <= 0:
            return
        if _np is not None:
            a = _np.frombuffer(buf, dtype=_np.float32, count=frames * 2).reshape(frames, 2)
            if abs(g1 - g0) < 1e-6:
                a *= _np.float32(g1)
            else:
                a *= _np.linspace(g0, g1, frames, dtype=_np.float32)[:, None]
            return
        arr = _array('f')
        arr.frombytes(bytes(buf[:frames * 8]))
        step = (g1 - g0) / frames
        for i in range(frames):
            g = g0 + step * i
            arr[2 * i] *= g
            arr[2 * i + 1] *= g
        buf[:frames * 8] = arr.tobytes()

    @staticmethod
    def _mix_layer_block(buf: bytearray, lbuf: bytearray, lg0: float, lg1: float) -> None:
        """Mix secondary stereo float32 PCM block into primary block with smooth gain ramp & headroom."""
        frames = min(len(buf), len(lbuf)) // 8
        if frames <= 0:
            return
        if _np is not None:
            a = _np.frombuffer(buf, dtype=_np.float32, count=frames * 2).reshape(frames, 2)
            b = _np.frombuffer(lbuf, dtype=_np.float32, count=frames * 2).reshape(frames, 2)
            if abs(lg1 - lg0) < 1e-6:
                g = _np.float32(lg1)
                hr = _np.float32(1.0 / math.sqrt(1.0 + float(lg1 * lg1) * 0.45))
                _np.clip((a + b * g) * hr, -0.95, 0.95, out=a)
            else:
                ramp = _np.linspace(lg0, lg1, frames, dtype=_np.float32)[:, None]
                mid_g = (lg0 + lg1) * 0.5
                hr = _np.float32(1.0 / math.sqrt(1.0 + mid_g * mid_g * 0.45))
                _np.clip((a + b * ramp) * hr, -0.95, 0.95, out=a)
            return
        arr_a = _array('f')
        arr_b = _array('f')
        arr_a.frombytes(bytes(buf[:frames * 8]))
        arr_b.frombytes(bytes(lbuf[:frames * 8]))
        step = (lg1 - lg0) / frames
        mid_g = (lg0 + lg1) * 0.5
        hr = 1.0 / math.sqrt(1.0 + mid_g * mid_g * 0.45)
        for i in range(frames):
            g = lg0 + step * i
            arr_a[2 * i] = max(-0.95, min(0.95, (arr_a[2 * i] + arr_b[2 * i] * g) * hr))
            arr_a[2 * i + 1] = max(-0.95, min(0.95, (arr_a[2 * i + 1] + arr_b[2 * i + 1] * g) * hr))
        buf[:frames * 8] = arr_a.tobytes()

    def _ambient_public_locked(self):
        ap = getattr(self, 'ambient_preset', None)
        if not ap or ap not in AMBIENT_PRESETS:
            return None
        meta = AMBIENT_PRESETS[ap]
        lay = getattr(self, 'ambient_layer', None)
        lay_meta = AMBIENT_PRESETS.get(lay) if (lay and lay != ap) else None
        lg = round(float(getattr(self, 'ambient_layer_gain', 0.45)), 2)
        aseq = int(getattr(self, 'ambient_seq', 0))
        if lay_meta:
            return {
                'preset': ap,
                'seq': aseq,
                'name': f'{meta["name"]} + {lay_meta["name"]}',
                'primary_name': meta['name'],
                'icon': meta['icon'],
                'desc': f'{meta["name"]}叠加{lay_meta["name"]}（{int(round(lg * 100))}%）· 双层立体声混音',
                'layer': lay,
                'layer_name': lay_meta['name'],
                'layer_icon': lay_meta['icon'],
                'layer_gain': lg,
            }
        return {
            'preset': ap,
            'seq': aseq,
            'name': meta['name'],
            'primary_name': meta['name'],
            'icon': meta['icon'],
            'desc': meta['desc'],
            'layer': '',
            'layer_name': '',
            'layer_icon': '',
            'layer_gain': lg,
        }

    def set_ambient(self, preset: str, volume=None, layer=None, layer_gain=None) -> dict:
        p = str(preset or '').strip().lower()
        if p in ('', 'off', 'stop', 'none', 'cancel', '0', 'false', '关闭', '停止', '静音'):
            if volume is not None and not isinstance(volume, bool):
                self.set_volume(volume)
            had = False
            flush = False
            with self._lock:
                if getattr(self, 'ambient_preset', None):
                    had = True
                    self._clear_duck_except_home_locked()
                    self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
                    self.ambient_preset = None
                    self.ambient_layer = None
                    self._ambient_buf = None
                    self._ambient_layer_buf = None
                    self._ambient_worker_gen = -1
                    flush = self._stop_proc_locked()
            if flush:
                self._send_aec_flush()
            return {'ok': True, 'ambient': None, 'message': '已关闭环境音' if had else '环境音未开启'}
        recipe_alias = {
            '深度自习': 'deep_study', '沉浸自习': 'deep_study',
            '围炉夜读': 'cozy_read', '阅读放空': 'cozy_read',
            '深度入眠': 'deep_sleep', '深度助眠': 'deep_sleep',
        }
        alias = {
            '雨声': 'rain', '细雨': 'rain', '窗外细雨': 'rain', '下雨': 'rain', '下雨声': 'rain',
            '海浪': 'waves', '大海': 'waves', '海岸慢浪': 'waves', '潮汐': 'waves', '海浪声': 'waves',
            '森林': 'forest', '微风': 'forest', '林间微风': 'forest', '山谷微风': 'forest', '风声': 'forest', '鸟鸣': 'forest',
            '溪流': 'stream', '流水': 'stream', '山涧溪流': 'stream', '溪水': 'stream',
            '虫鸣': 'night', '夏夜虫鸣': 'night', '白噪音': 'white_noise', '柔和白噪音': 'white_noise',
            '褐噪': 'brown_noise', '褐噪音': 'brown_noise', '静心褐噪': 'brown_noise', '低沉褐噪音': 'brown_noise', '棕噪音': 'brown_noise',
            '粉噪音': 'pink_noise', '粉红噪音': 'pink_noise', '粉噪': 'pink_noise',
            '轻琴': 'sleep_piano', '雨夜轻琴': 'sleep_piano', '钢琴': 'sleep_piano', '助眠': 'sleep_piano', '催眠': 'sleep_piano',
            '壁炉': 'fireplace', '柴火': 'fireplace', '壁炉柴火': 'fireplace', '篝火': 'fireplace', '烤火': 'fireplace', 'fire': 'fireplace',
            '书房': 'library', '图书馆': 'library', '翻书': 'library', '写字': 'library', '书房自习': 'library', '自习室': 'library',
            '颂钵': 'singing_bowl', '禅意颂钵': 'singing_bowl', '冥想': 'singing_bowl', '钵音': 'singing_bowl', 'bowl': 'singing_bowl',
        }
        rec_key = recipe_alias.get(p, p)
        if rec_key in AMBIENT_RECIPES:
            rec = AMBIENT_RECIPES[rec_key]
            p = rec['preset']
            if layer is None:
                layer = rec['layer']
            if layer_gain is None:
                layer_gain = rec['layer_gain']
        p = alias.get(p, p)
        if p not in AMBIENT_PRESETS:
            return {'ok': False, 'error': f'未知环境音: {preset}'}
        lay = None
        if layer is not None:
            if isinstance(layer, bool):
                return {'ok': False, 'error': '无效叠加音'}
            l_raw = str(layer).strip().lower()
            if l_raw not in ('', 'off', 'stop', 'none', 'cancel', '0', '关闭', '不叠加'):
                l_res = alias.get(l_raw, l_raw)
                if l_res not in AMBIENT_PRESETS:
                    return {'ok': False, 'error': f'未知叠加音: {layer}'}
                if l_res != p:
                    lay = l_res
        lg = getattr(self, 'ambient_layer_gain', 0.45)
        if layer_gain is not None:
            if isinstance(layer_gain, bool):
                return {'ok': False, 'error': '无效叠加比例'}
            try:
                lg_f = float(layer_gain)
            except (TypeError, ValueError, OverflowError):
                return {'ok': False, 'error': '无效叠加比例'}
            if not math.isfinite(lg_f):
                return {'ok': False, 'error': '无效叠加比例'}
            if lg_f > 1.001:
                lg_f = lg_f / 100.0
            lg = round(max(0.10, min(1.00, lg_f)), 2)
        if volume is not None and not isinstance(volume, bool):
            self.set_volume(volume)

        with self._lock:
            self._clear_duck_except_home_locked()
            if getattr(self, 'sleep_mode', None) == 'tracks':
                m_conv = max(15, min(240, max(1, int(getattr(self, 'sleep_tracks_left', 1) or 1)) * 15))
                self.sleep_mode = 'time'
                self.sleep_deadline = time.monotonic() + m_conv * 60
                self.sleep_total = m_conv
                self.sleep_tracks_left = 0
            if (
                getattr(self, 'ambient_preset', None) == p
                and getattr(self, 'ambient_layer', None) == lay
                and getattr(self, '_ambient_worker_gen', -1) == self._gen
                and not getattr(self, '_shutting_down', False)
            ):
                self.ambient_layer_gain = lg
                self.just_started = True
                pub = self._ambient_public_locked()
                vol = self.volume
                return {'ok': True, 'ambient': pub, 'volume': vol, 'message': f'正在播放{pub["icon"]} {pub["name"]}'}

        m4a_path = _ambient_file_for(p)
        pcm_bytes = None
        if m4a_path is None:
            cached = getattr(self, '_ambient_buf', None)
            pcm_bytes = cached[1] if (isinstance(cached, tuple) and cached[0] == p) else _fallback_ambient_pcm(p)
        lay_m4a = _ambient_file_for(lay) if lay else None
        lay_pcm = None
        if lay and lay_m4a is None:
            lcached = getattr(self, '_ambient_layer_buf', None)
            lay_pcm = lcached[1] if (isinstance(lcached, tuple) and lcached[0] == lay) else _fallback_ambient_pcm(lay)
        paused_song = False
        with self._lock:
            self._clear_duck_except_home_locked()
            if getattr(self, 'sleep_mode', None) == 'tracks':
                m_conv = max(15, min(240, max(1, int(getattr(self, 'sleep_tracks_left', 1) or 1)) * 15))
                self.sleep_mode = 'time'
                self.sleep_deadline = time.monotonic() + m_conv * 60
                self.sleep_total = m_conv
                self.sleep_tracks_left = 0
            had_proc = self._stop_proc_locked()
            if self.playing and not self.paused:
                self.paused = True
                paused_song = True
            self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
            self.ambient_preset = p
            self.ambient_layer = lay
            self.ambient_layer_gain = lg
            if pcm_bytes is not None:
                self._ambient_buf = (p, pcm_bytes)
            if lay_pcm is not None:
                self._ambient_layer_buf = (lay, lay_pcm)
            self.just_started = True
            gen = self._gen
            self._ambient_worker_gen = gen
            pub = self._ambient_public_locked()
            vol = self.volume
        if had_proc:
            self._send_aec_flush()
        if paused_song:
            self.save_session()
        self._pause_bluetooth_if_streaming()
        threading.Thread(
            target=self._ambient_worker,
            args=(gen, p, m4a_path, pcm_bytes, lay, lay_m4a, lay_pcm),
            daemon=True,
        ).start()
        return {'ok': True, 'ambient': pub, 'volume': vol, 'message': f'正在播放{pub["icon"]} {pub["name"]}'}

    def _ambient_worker(self, gen: int, preset: str, m4a_path, pcm_bytes, layer: str = None, lay_m4a=None, lay_pcm=None):
        block = 960 * 2 * 4  # 7680 bytes = 20ms @ 48kHz stereo float32
        env = {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000', 'PULSE_SERVER': 'unix:/run/user/1000/pulse/native'}
        proc = None
        proc2 = None
        pacat = None
        try:
            if m4a_path is not None:
                try:
                    proc = subprocess.Popen(
                        [
                            'ffmpeg', '-loglevel', 'error', '-nostdin',
                            '-stream_loop', '-1', '-i', str(m4a_path),
                            '-f', 'f32le', '-ac', '2', '-ar', str(OUT_RATE), 'pipe:1',
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    with self._lock:
                        if (
                            gen != self._gen
                            or getattr(self, 'ambient_preset', None) != preset
                            or getattr(self, 'ambient_layer', None) != layer
                            or getattr(self, '_shutting_down', False)
                        ):
                            return
                        self._proc = proc
                except Exception as e:
                    log.warning('Ambient ffmpeg decode fallback (%s): %s', preset, e)
                    if proc is not None:
                        with contextlib.suppress(Exception):
                            proc.kill()
                            proc.wait(timeout=1)
                        if proc.stdout:
                            with contextlib.suppress(Exception):
                                proc.stdout.close()
                    proc = None
                    if pcm_bytes is None:
                        pcm_bytes = _fallback_ambient_pcm(preset)

            if layer and lay_m4a is not None:
                try:
                    proc2 = subprocess.Popen(
                        [
                            'ffmpeg', '-loglevel', 'error', '-nostdin',
                            '-stream_loop', '-1', '-i', str(lay_m4a),
                            '-f', 'f32le', '-ac', '2', '-ar', str(OUT_RATE), 'pipe:1',
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    with self._lock:
                        if (
                            gen != self._gen
                            or getattr(self, 'ambient_preset', None) != preset
                            or getattr(self, 'ambient_layer', None) != layer
                            or getattr(self, '_shutting_down', False)
                        ):
                            return
                        self._proc2 = proc2
                except Exception as e:
                    log.warning('Ambient layer ffmpeg fallback (%s): %s', layer, e)
                    if proc2 is not None:
                        with contextlib.suppress(Exception):
                            proc2.kill()
                            proc2.wait(timeout=1)
                        if proc2.stdout:
                            with contextlib.suppress(Exception):
                                proc2.stdout.close()
                    proc2 = None
                    if lay_pcm is None:
                        lay_pcm = _fallback_ambient_pcm(layer)

            total = ((len(pcm_bytes) // block) * block) if pcm_bytes else 0
            lay_total = ((len(lay_pcm) // block) * block) if lay_pcm else 0
            if proc is None and total <= 0:
                return
            pos = 0
            lay_pos = 0
            buf = bytearray(block)
            mv = memoryview(buf)
            lbuf = bytearray(block) if layer else None
            lmv = memoryview(lbuf) if lbuf is not None else None
            fade_prev = 0.0
            lg_prev = float(getattr(self, 'ambient_layer_gain', 0.45))
            blk_idx = 0
            next_t = time.monotonic() - 0.08
            pacat_retry_mono = 0.0
            while True:
                with self._lock:
                    if (
                        gen != self._gen
                        or getattr(self, 'ambient_preset', None) != preset
                        or getattr(self, 'ambient_layer', None) != layer
                        or getattr(self, '_shutting_down', False)
                    ):
                        break
                    muted = bool(self.alarm_ringing or self.ducked or (time.monotonic() < getattr(self, 'study_duck_until', 0.0)))
                    sleep_g = self._sleep_gain_locked()
                    cur_lg = float(getattr(self, 'ambient_layer_gain', 0.45))
                if muted:
                    fade_prev = 0.0
                    blk_idx = 0
                    time.sleep(0.04)
                    next_t = time.monotonic() - 0.08
                    continue

                if proc is not None:
                    got = self._readinto_exact(proc.stdout, mv)
                    if not got:
                        with self._lock:
                            stopped = (
                                gen != self._gen
                                or getattr(self, 'ambient_preset', None) != preset
                                or getattr(self, 'ambient_layer', None) != layer
                                or getattr(self, '_shutting_down', False)
                            )
                        if stopped:
                            break
                        log.warning('Ambient ffmpeg runtime EOF fallback (%s)', preset)
                        with contextlib.suppress(Exception):
                            proc.kill()
                            proc.wait(timeout=1)
                        if proc.stdout:
                            with contextlib.suppress(Exception):
                                proc.stdout.close()
                        with self._lock:
                            if getattr(self, '_proc', None) is proc:
                                self._proc = None
                        proc = None
                        if pcm_bytes is None:
                            pcm_bytes = _fallback_ambient_pcm(preset)
                            total = (len(pcm_bytes) // block) * block
                        if total <= 0:
                            break
                        buf[:] = pcm_bytes[pos:pos + block]
                        pos = (pos + block) % total
                        next_t = time.monotonic() - 0.08
                    elif got < block:
                        aligned = (got // 8) * 8
                        mv[aligned:] = b'\x00' * (block - aligned)
                else:
                    buf[:] = pcm_bytes[pos:pos + block]
                    pos += block
                    if pos >= total:
                        pos = 0

                if layer and lbuf is not None:
                    has_lay = False
                    if proc2 is not None:
                        lgot = self._readinto_exact(proc2.stdout, lmv)
                        if lgot:
                            if lgot < block:
                                laligned = (lgot // 8) * 8
                                lmv[laligned:] = b'\x00' * (block - laligned)
                            has_lay = True
                        else:
                            with self._lock:
                                lstopped = (
                                    gen != self._gen
                                    or getattr(self, 'ambient_preset', None) != preset
                                    or getattr(self, 'ambient_layer', None) != layer
                                    or getattr(self, '_shutting_down', False)
                                )
                            if lstopped:
                                break
                            with contextlib.suppress(Exception):
                                proc2.kill()
                                proc2.wait(timeout=1)
                            if proc2.stdout:
                                with contextlib.suppress(Exception):
                                    proc2.stdout.close()
                            with self._lock:
                                if getattr(self, '_proc2', None) is proc2:
                                    self._proc2 = None
                            proc2 = None
                            if lay_pcm is None:
                                lay_pcm = _fallback_ambient_pcm(layer)
                                lay_total = (len(lay_pcm) // block) * block
                            next_t = time.monotonic()
                    if proc2 is None and lay_total > 0:
                        lbuf[:] = lay_pcm[lay_pos:lay_pos + block]
                        lay_pos += block
                        if lay_pos >= lay_total:
                            lay_pos = 0
                        has_lay = True
                    if has_lay:
                        self._mix_layer_block(buf, lbuf, lg_prev, cur_lg)
                    lg_prev = cur_lg

                blk_idx += 1
                fade_in = min(1.0, blk_idx / 40.0)  # silky 0.8s fade-in on start / resume
                tgt_g = fade_in * sleep_g
                if tgt_g < 0.9999 or fade_prev < 0.9999:
                    self._apply_gain(buf, fade_prev, tgt_g)
                fade_prev = tgt_g

                sent_to_aec = False
                with self._aec_lock:
                    for sock in list(self.aec_clients):
                        try:
                            sock.sendall(mv)
                            sent_to_aec = True
                        except Exception:
                            self.aec_clients.discard(sock)
                            with contextlib.suppress(Exception):
                                sock.close()
                if not sent_to_aec:
                    try:
                        if pacat is None and time.monotonic() >= pacat_retry_mono:
                            pacat = subprocess.Popen(
                                ['pacat', '--rate=48000', '--channels=2', '--format=float32le', '--latency-msec=60'],
                                stdin=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                env=env,
                            )
                            with self._lock:
                                if gen != self._gen or getattr(self, 'ambient_preset', None) != preset or getattr(self, 'ambient_layer', None) != layer:
                                    break
                                self._pacat = pacat
                        if pacat is not None:
                            pacat.stdin.write(mv)
                            pacat.stdin.flush()
                    except OSError:
                        pacat_retry_mono = time.monotonic() + 0.3
                        if pacat is not None:
                            with contextlib.suppress(Exception):
                                pacat.kill()
                                pacat.wait(timeout=1)
                            if pacat.stdin:
                                with contextlib.suppress(Exception):
                                    pacat.stdin.close()
                            pacat = None
                        with self._lock:
                            if self._pacat is not None and gen == self._gen:
                                self._pacat = None
                    if pacat is None and proc is not None:
                        time.sleep(0.02)
                elif pacat is not None:
                    with contextlib.suppress(Exception):
                        pacat.kill()
                        pacat.wait(timeout=1)
                    if pacat.stdin:
                        with contextlib.suppress(Exception):
                            pacat.stdin.close()
                    pacat = None
                    with self._lock:
                        if self._pacat is not None and gen == self._gen:
                            self._pacat = None

                if proc is None:
                    next_t += 0.02
                    rem = next_t - time.monotonic()
                    if rem > 0.001:
                        time.sleep(rem)
                    elif rem < -0.15:
                        next_t = time.monotonic()
        finally:
            with self._lock:
                if proc is not None and self._proc is proc:
                    self._proc = None
                if proc2 is not None and getattr(self, '_proc2', None) is proc2:
                    self._proc2 = None
                if getattr(self, '_ambient_worker_gen', -1) == gen:
                    self._ambient_worker_gen = -1
                if self._pacat is pacat:
                    self._pacat = None
            for p_sub in (proc, proc2, pacat):
                if p_sub is not None:
                    with contextlib.suppress(Exception):
                        p_sub.kill()
                        p_sub.wait(timeout=1)
                    for s in (getattr(p_sub, 'stdin', None), getattr(p_sub, 'stdout', None), getattr(p_sub, 'stderr', None)):
                        if s is not None:
                            with contextlib.suppress(Exception):
                                s.close()

    def sleep_tick(self) -> bool:
        """Called twice a second: pause once the 'time' sleep timer has run out."""
        with self._lock:
            if self.sleep_mode != 'time' or time.monotonic() < self.sleep_deadline:
                return False
            self._clear_sleep_locked()
            active = self.playing and not self.paused
            had_amb = bool(getattr(self, 'ambient_preset', None))
        if had_amb:
            self.set_ambient('off')
        if active:
            self.control('pause')
        log.info('Sleep timer reached -> %s', 'paused' if (active or had_amb) else 'nothing playing')
        self._preview_end()
        self._sleep_session_end('timer')
        return True

    # ---- 2026-11-12 (Claude): sleep sessions ---------------------------------------------------------------------
    # One call: a bedtime sound, its own volume, a timer with a long fade (or until a time / all night). The volume the
    # family uses in the day comes back at 06:30 when the sound stopped in the night (00:00-06:30), else right away,
    # unless someone changed it meanwhile.
    def _sleep_session_public_locked(self):
        ss = getattr(self, 'sleep_session', None)
        if not ss or ss.get('stopping'):
            return None
        timed = self.sleep_mode == 'time'
        return {'label': ss['label'], 'kind': ss['sound']['kind'], 'preset': ss['sound'].get('preset') or '',
                'playlist': ss['sound'].get('id') or '', 'night': bool(ss.get('night')), 'volume': ss['volume'],
                'fade': int(ss['fade']), 'started': int(ss['started']),
                'ends_at': int(self.sleep_ends_wall) if (timed and self.sleep_ends_wall) else None,
                'remaining': int(math.ceil(max(0.0, self.sleep_deadline - time.monotonic()))) if timed else None}

    def sleep_session_info(self) -> dict:
        with self._lock:
            pub = self._sleep_session_public_locked()
            st = dict(self._sleep_state)
        last = st.get('last') if isinstance(st.get('last'), dict) else None
        return {'ok': True, 'session': pub, 'last': last,
                'sleep_volume': int(st.get('sleep_volume') or SLEEP_DEFAULT_VOLUME),
                'restore': st.get('restore') if isinstance(st.get('restore'), dict) else None,
                'presets': [dict(p, mid=None) for p in AMBIENT_PRESETS.values()],
                'recipes': list(AMBIENT_RECIPES.values()), 'playlists': list(SLEEP_PLAYLISTS),
                'combos': [{'preset': m, 'layer': l, 'layer_gain': g, 'name': n} for m, l, g, n in SLEEP_COMBOS],
                'morning': '%02d:%02d' % SLEEP_MORNING, 'night_from': '%02d:%02d' % SLEEP_DAY_END,
                'wake': st.get('wake') is not False, 'wake_time': '%02d:%02d' % _wake_hm(st), 'wake_next': _wake_next(st),
                'wake_volume': list(SLEEP_WAKE_VOLUME), 'wake_screen_max': SLEEP_WAKE_SCREEN_MAX}

    def sleep_session_start(self, sound=None, minutes=None, until=None, night=False, fade=None, volume=None) -> dict:
        st = self._sleep_state
        last = st.get('last') if isinstance(st.get('last'), dict) else {}
        use_last = sound in (None, '', 'last', '上次')
        snd = _clean_sleep_sound(last.get('sound') if use_last else sound)
        if snd is None:
            if not use_last:
                return {'ok': False, 'error': f'没有找到助眠声音：{sound}'}
            snd = {'kind': 'ambient', 'preset': 'rain', 'layer': 'fireplace', 'layer_gain': 0.4}
        timer = {}
        if night is True:
            timer = {'night': True}
        elif until:
            timer = {'until': str(until)}
        elif minutes is not None:
            timer = {'minutes': minutes}
        elif use_last and isinstance(last.get('timer'), dict):
            timer = dict(last['timer'])
        if not timer:
            timer = {'minutes': SLEEP_DEFAULT_MINUTES}
        if fade is None:
            fade = last.get('fade', SLEEP_DEFAULT_FADE) if use_last else SLEEP_DEFAULT_FADE
        try:
            vol = int(round(float(volume if volume is not None else st.get('sleep_volume') or SLEEP_DEFAULT_VOLUME)))
        except (TypeError, ValueError, OverflowError):
            vol = SLEEP_DEFAULT_VOLUME
        vol = max(5, min(100, vol))
        if snd['kind'] == 'current':
            with self._lock:
                sounding = bool(getattr(self, 'ambient_preset', None)) or (self.playing and not self.paused)
            if not sounding:
                return {'ok': False, 'error': '现在没有在播放，先选一个助眠声音吧'}
        tracks = None
        if snd['kind'] == 'playlist':
            tracks = qq_playlist_tracks(snd['id'], 160)
            random.shuffle(tracks)
            tracks = _qq_playable_tracks(tracks)[:60]
            if not tracks:
                return {'ok': False, 'error': '这个助眠歌单暂时放不了，换一个试试'}
        # the volume first, so the sound never starts loud
        with self._lock:
            pv = getattr(self, '_preview', None)
            self._preview = None  # a session started during a preview takes over; the day volume is the one from before it
            cur_vol = pv['prev_volume'] if pv else self.volume
            r = st.get('restore') if isinstance(st.get('restore'), dict) else None
        day = int(r['day_volume']) if (r and type(r.get('day_volume')) is int) else cur_vol
        st['restore'] = {'day_volume': day, 'sleep_volume': vol, 'at': None}
        self.set_volume(vol)
        if snd['kind'] == 'ambient':
            res = self.set_ambient(snd['preset'], None, snd.get('layer') or ('off' if snd['preset'] in AMBIENT_PRESETS else None),
                                   snd.get('layer_gain'))
        elif snd['kind'] == 'playlist':
            res = self.play_tracks(tracks, 0)
        else:
            res = {'ok': True}
        if not res.get('ok'):
            _write_sleep_state(st)
            self._sleep_session_end('failed')
            return {'ok': False, 'error': res.get('error') or '声音没能开始'}
        with self._lock:
            self._sg_last = (1.0, time.monotonic())
        tres = self.set_sleep(timer.get('minutes'), None, False, fade, timer.get('until'), timer.get('night') is True)
        if not tres.get('ok'):
            tres = self.set_sleep(SLEEP_DEFAULT_MINUTES, None, False, fade)
            timer = {'minutes': SLEEP_DEFAULT_MINUTES}
        label = _sleep_sound_label(snd)
        with self._lock:
            self.sleep_session = {'sound': snd, 'label': label, 'started': time.time(), 'night': timer.get('night') is True,
                                  'fade': float(getattr(self, 'sleep_fade', SLEEP_DEFAULT_FADE)), 'volume': vol}
            self._sleep_idle_since = None
            pub = self._sleep_session_public_locked()
        if snd['kind'] != 'current':
            st['last'] = {'sound': snd, 'timer': timer, 'fade': fade}
        st['sleep_volume'] = vol
        st['last_started'] = int(time.time())   # 温柔叫醒 runs the morning after a night with sleep mode
        _write_sleep_state(st)
        log.info('Sleep session: %s, %s, volume %d (day %d)', label, timer, vol, day)
        msg = f'好的，{label}。' + str(tres.get('message') or '').replace('好的，', '') + f'。音量调到 {vol}%，晚安'
        return {'ok': True, 'session': pub, 'message': msg}

    def sleep_session_extend(self, minutes=30) -> dict:
        try:
            add = max(5, min(120, int(float(minutes))))
        except (TypeError, ValueError, OverflowError):
            add = 30
        with self._lock:
            if self.sleep_mode != 'time':
                return {'ok': False, 'error': '现在没有定时'}
            if self.sleep_night:
                return {'ok': False, 'error': '整夜模式会在早上自己停'}
            now = time.monotonic()
            left = max(self.sleep_deadline, now) - now
            self.sleep_deadline = now + left + add * 60
            self.sleep_total = max(1, int(math.ceil((self.sleep_deadline - self.sleep_started_mono) / 60.0)))
            self.sleep_ends_wall = time.time() + (self.sleep_deadline - now)
            pub = self._sleep_session_public_locked()
            end_txt = time.strftime('%H:%M', time.localtime(self.sleep_ends_wall))
        log.info('Sleep timer extended by %d min (ends %s)', add, end_txt)
        return {'ok': True, 'session': pub, 'message': f'好的，再放 {add} 分钟，{end_txt} 停止'}

    def sleep_session_stop(self) -> dict:
        with self._lock:
            sounding = bool(getattr(self, 'ambient_preset', None)) or (self.playing and not self.paused)
            if sounding:  # a short fade; sleep_tick stops the sound and ends the session
                if isinstance(getattr(self, 'sleep_session', None), dict):
                    self.sleep_session['stopping'] = True
                self.sleep_mode = 'time'
                self.sleep_deadline = time.monotonic() + 2.5
                self.sleep_fade = 2.5
                self.sleep_night = False
                self.sleep_tracks_left = 0
            else:
                self._clear_sleep_locked()
        if not sounding:
            self._sleep_session_end('stop')
        return {'ok': True, 'message': '好的，已停止'}

    def _sleep_session_end(self, why: str):
        with self._lock:
            had = getattr(self, 'sleep_session', None) is not None
            self.sleep_session = None
            self._sleep_idle_since = None
        st = getattr(self, '_sleep_state', None)
        r = st.get('restore') if isinstance(st, dict) else None
        if isinstance(r, dict) and r.get('at') is None:
            r['at'] = _next_sleep_restore_at()
            _write_sleep_state(self._sleep_state)
            log.info('Sleep session ended (%s); day volume %s back at %s', why, r.get('day_volume'),
                     time.strftime('%m-%d %H:%M', time.localtime(r['at'])))
        elif had:
            log.info('Sleep session ended (%s)', why)

    # ---- 试听 (11-12): the chosen sound at the sleep volume for half a minute; no session, no timer shown, no dark screen
    def _preview_public_locked(self):
        pv = getattr(self, '_preview', None)
        if not pv or self.sleep_mode != 'time':
            return None
        return {'label': pv['label'], 'remaining': int(math.ceil(max(0.0, self.sleep_deadline - time.monotonic())))}

    def sleep_preview(self, sound=None, volume=None, seconds=30) -> dict:
        with self._lock:
            if getattr(self, 'sleep_session', None) is not None:
                return {'ok': False, 'error': '睡眠正在进行，不用试听'}
        snd = _clean_sleep_sound(sound)
        if snd is None or snd['kind'] != 'ambient':
            return {'ok': False, 'error': '歌单不能试听，直接开始就能听' if (snd and snd['kind'] == 'playlist') else '没有找到这个声音'}
        try:
            vol = int(round(float(volume if volume is not None else self._sleep_state.get('sleep_volume') or SLEEP_DEFAULT_VOLUME)))
        except (TypeError, ValueError, OverflowError):
            vol = SLEEP_DEFAULT_VOLUME
        vol = max(5, min(100, vol))
        try:
            secs = max(5.0, min(60.0, float(seconds)))
        except (TypeError, ValueError, OverflowError):
            secs = 30.0
        with self._lock:
            pv = getattr(self, '_preview', None)
            prev = pv['prev_volume'] if pv else self.volume
            self._preview = {'prev_volume': prev, 'volume': vol, 'label': _sleep_sound_label(snd)}
            cur = self.volume
        if cur != vol:
            self.set_volume(vol)
        res = self.set_ambient(snd['preset'], None, snd.get('layer') or ('off' if snd['preset'] in AMBIENT_PRESETS else None),
                               snd.get('layer_gain'))
        if not res.get('ok'):
            self._preview_end()
            return {'ok': False, 'error': res.get('error') or '声音没能播放'}
        with self._lock:
            now = time.monotonic()
            self.sleep_mode = 'time'
            self.sleep_deadline = now + secs
            self.sleep_total = 1
            self.sleep_tracks_left = 0
            self.sleep_fade = 2.0
            self.sleep_night = False
            self.sleep_started_mono = now
            self.sleep_ends_wall = time.time() + secs
            self._sg_last = (1.0, now)
            pub = self._preview_public_locked()
        log.info('Sleep preview: %s at volume %d for %ds', pub['label'] if pub else '?', vol, int(secs))
        return {'ok': True, 'preview': pub, 'message': f'试听：{pub["label"] if pub else ""}'}

    def sleep_preview_stop(self) -> dict:
        with self._lock:
            if getattr(self, '_preview', None) is None:
                return {'ok': True, 'message': '没有在试听'}
            now = time.monotonic()
            if self.sleep_mode == 'time' and self.sleep_deadline > now + 1.2:
                self.sleep_deadline = now + 1.2  # a short fade; sleep_tick stops it and gives the volume back
                self.sleep_fade = 1.2
        return {'ok': True, 'message': '已停止试听'}

    def _preview_end(self):
        with self._lock:
            pv = getattr(self, '_preview', None)
            if pv is None:
                return
            self._preview = None
            cur = self.volume
        if pv and cur == pv['volume'] and pv['prev_volume'] != cur:
            self.set_volume(pv['prev_volume'])  # the volume from before the preview (unless it was changed meanwhile)

    def sleep_screen_off(self) -> dict:
        with self._lock:
            active = getattr(self, 'sleep_session', None) is not None
        if not active and not _is_sleep_evening():
            return {'ok': False, 'error': '睡眠模式没在进行'}
        try:
            BACKLIGHT_PATH.write_text(f'{SLEEP_BLANK}\n')  # the idle blanker pauses the renderer; a touch wakes it
        except OSError as e:
            return {'ok': False, 'error': f'熄屏失败：{type(e).__name__}'}
        return {'ok': True}

    def sleep_night_view(self, on) -> dict:
        """The night view is up (or gone): the blanker keeps the screen at its lowest level while it is up."""
        try:
            if on is True:
                _write_runtime_json(NIGHT_VIEW_PATH, {'until': int(time.time() + 12 * 3600)})
            else:
                NIGHT_VIEW_PATH.unlink(missing_ok=True)
        except OSError as e:
            return {'ok': False, 'error': f'夜间画面状态没写成：{type(e).__name__}'}
        return {'ok': True}

    def sleep_set_wake(self, on=None, time_s=None) -> dict:
        """The 温柔叫醒 switch and/or its time ('HH:MM', 04:30-10:00)."""
        st = self._sleep_state
        if on is None and time_s is None:
            return {'ok': False, 'error': '没有要改的'}
        if on is not None and not isinstance(on, bool):
            return {'ok': False, 'error': '开关只能是开或关'}
        if time_s is not None:
            m = re.match(r'^(\d{1,2}):(\d{2})$', str(time_s))
            hm = (int(m.group(1)), int(m.group(2))) if m else None
            if not hm or hm[1] > 59 or not ((4, 30) <= hm <= (10, 0)):
                return {'ok': False, 'error': '叫醒时间要在 4:30 到 10:00 之间'}
            st['wake_time'] = '%02d:%02d' % hm
        if on is not None:
            st['wake'] = on
        _write_sleep_state(st)
        if st.get('wake') is False and getattr(self, '_wake', None) is not None:
            self._wake_stop_soon('the switch')
        nxt = _wake_next(st)
        log.info('Sleep: 温柔叫醒 %s at %s', 'off' if st.get('wake') is False else 'on', nxt['time'])
        if st.get('wake') is False:
            msg = '温柔叫醒已关闭'
        elif nxt['off']:
            msg = f'温柔叫醒定在 {nxt["time"]}；{nxt["when"]}是{nxt["why"] or "休息日"}，不叫'
        else:
            msg = f'温柔叫醒定在 {nxt["time"]}（工作日）'
        return {'ok': True, 'wake': st.get('wake') is not False, 'wake_time': '%02d:%02d' % _wake_hm(st), 'wake_next': nxt, 'message': msg}

    def _wake_stop_soon(self, why):
        """A touch / the switch: the birds go in 2 seconds, the screen goes back."""
        with self._lock:
            wk = getattr(self, '_wake', None)
            if wk is None or wk.get('stopping'):
                return
            now = time.monotonic()
            wk['stopping'] = why
            wk['fade'] = 2.0
            wk['end'] = min(wk['end'], now + 2.0)
        _sunrise_mark_stopped()
        log.info('温柔叫醒: stopped (%s)', why)

    def sleep_wake_tick(self):
        """Every 2 s. At the user's time (default 06:30), on a workday by the calendar, the morning after a night with
        sleep mode: the volume goes 20% -> 50% and birds play; the screen owner brightens the screen up to 50%.
        Not over music, an alarm, a call or AirPlay; a sleep sound still on makes way; once a morning."""
        st = self._sleep_state
        wk = getattr(self, '_wake', None)
        if wk is not None:
            self._wake_step(wk)
            return
        now = time.time()
        if NIGHT_VIEW_PATH.exists() and not _is_sleep_evening(now) and getattr(self, 'sleep_session', None) is None:
            with contextlib.suppress(OSError):
                NIGHT_VIEW_PATH.unlink()
        if st.get('wake') is False:
            return
        h, mi = _wake_hm(st)
        lt = time.localtime(now)
        at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        day = time.strftime('%Y-%m-%d', lt)
        if not (at <= now < at + 900) or st.get('wake_done') == day:
            return
        started = st.get('last_started')
        if not (type(started) in (int, float) and at - 12.5 * 3600 <= started < at):
            return
        off, why = _wake_day_info(lt.tm_year, lt.tm_mon, lt.tm_mday)
        if off:
            st['wake_done'] = day
            _write_sleep_state(st)
            log.info('温柔叫醒 skipped: a day off (%s)', why or 'calendar')
            return
        with self._lock:
            ss = getattr(self, 'sleep_session', None)
            alarm = getattr(self, 'alarm_ringing', False)
            music = getattr(self, 'playing', False) and not getattr(self, 'paused', False)
            preview = getattr(self, '_preview', None) is not None
            other = bool(getattr(self, 'ambient_preset', None)) and ss is None
        if ss is not None and not alarm:   # the sleep sound is still on (all night / a long timer): it makes way
            if not ss.get('stopping'):
                log.info('温柔叫醒: the sleep sound makes way')
                self.sleep_session_stop()
            return
        st['wake_done'] = day
        _write_sleep_state(st)
        if alarm or music or preview or other or CALL_FLAG_PATH.exists() or MIRROR_FLAG_PATH.exists():
            log.info('温柔叫醒 skipped: something is playing, an alarm, a call or AirPlay')
            return
        r = st.get('restore')
        day_vol = int(r['day_volume']) if isinstance(r, dict) and type(r.get('day_volume')) is int else int(getattr(self, 'volume', 50))
        st['restore'] = None   # the wake-up gives the day volume back itself, when it is over
        _write_sleep_state(st)
        v0 = SLEEP_WAKE_VOLUME[0]
        mono = time.monotonic()
        with self._lock:
            self._sg_last = (0.003, mono)
            self._wake = {'start': mono, 'ramp': float(SLEEP_WAKE_RAMP), 'preset': SLEEP_WAKE_SOUND,
                          'end': mono + SLEEP_WAKE_RAMP + SLEEP_WAKE_HOLD + SLEEP_WAKE_FADE, 'fade': float(SLEEP_WAKE_FADE),
                          'day_vol': day_vol, 'vol': v0, 'user_vol': False, 'sun': int(now)}
        self.set_volume(v0)
        with contextlib.suppress(OSError):
            _write_runtime_json(SUNRISE_PATH, {'start': int(now), 'ramp': SLEEP_WAKE_RAMP,
                                               'hold': SLEEP_WAKE_HOLD + SLEEP_WAKE_FADE, 'max': SLEEP_WAKE_SCREEN_MAX})
        res = self.set_ambient(SLEEP_WAKE_SOUND, None, 'off', None)
        if not res.get('ok'):
            self._wake = None
            _sunrise_mark_stopped()
            if getattr(self, 'volume', None) == v0:
                self.set_volume(day_vol)
            log.warning('温柔叫醒: the sound did not start (%s)', res.get('error'))
            return
        log.info('温柔叫醒: %s, volume %d%% -> %d%%, the screen up to %d%%', AMBIENT_PRESETS[SLEEP_WAKE_SOUND]['name'],
                 SLEEP_WAKE_VOLUME[0], SLEEP_WAKE_VOLUME[1], SLEEP_WAKE_SCREEN_MAX)

    def _wake_step(self, wk):
        """A wake-up in progress: the volume step, a touch, its end."""
        now_m = time.monotonic()
        with self._lock:
            mine = getattr(self, 'ambient_preset', None) == wk['preset']
            music = getattr(self, 'playing', False) and not getattr(self, 'paused', False)
            alarm = getattr(self, 'alarm_ringing', False)
            cur = int(getattr(self, 'volume', 0))
        if not wk.get('stopping'):
            touched = False
            try:
                data = json.loads(SUNRISE_PATH.read_text(encoding='utf-8'))
                touched = isinstance(data, dict) and data.get('start') == wk['sun'] and 'stopped' in data
            except (OSError, ValueError):
                pass
            if touched:
                self._wake_stop_soon('a touch on the screen')
                return
        if not mine or music or alarm or now_m >= wk['end']:
            self._wake = None
            if mine:
                self.set_ambient('off')
            if not wk.get('user_vol') and cur == wk['vol'] and wk['day_vol'] != cur:
                self.set_volume(wk['day_vol'])   # the volume from before (unless someone set one meanwhile)
            if now_m < wk['end'] or not mine:
                _sunrise_mark_stopped()
            log.info('温柔叫醒 finished%s', '' if mine and not music and not alarm else ' (the sound was changed / music / an alarm)')
            return
        if wk.get('user_vol') or wk.get('stopping'):
            return
        if cur != wk['vol']:   # someone set a volume: theirs from now on
            wk['user_vol'] = True
            return
        v0, v1 = SLEEP_WAKE_VOLUME
        want = int(round(v0 + (v1 - v0) * min(1.0, max(0.0, now_m - wk['start']) / wk['ramp'])))
        if want != cur:
            wk['vol'] = want
            self.set_volume(want)

    def on_alarm_ringing(self) -> bool:
        """When a wake-up alarm rings during a sleep session, stop bedtime audio immediately so it does not resume
        after the alarm is dismissed, and mark day_volume due as soon as alarm_ringing clears."""
        with self._lock:
            if getattr(self, 'sleep_session', None) is None:
                return False
            self._clear_sleep_locked()
            active = self.playing and not self.paused
            had_amb = bool(getattr(self, 'ambient_preset', None))
        if had_amb:
            self.set_ambient('off')
        if active:
            self.control('pause')
        self._sleep_session_end('alarm')
        st = getattr(self, '_sleep_state', None)
        r = st.get('restore') if isinstance(st, dict) else None
        if isinstance(r, dict):
            r['at'] = time.time()
            _write_sleep_state(st)
        return True

    def sleep_housekeeping(self):
        """Every 2 s: bring the day volume back when it is due; end a session whose sound was stopped another way."""
        if getattr(self, 'alarm_ringing', False) and getattr(self, 'sleep_session', None) is not None:
            self.on_alarm_ringing()
        r = self._sleep_state.get('restore')
        if not getattr(self, '_sleep_recovered', False):
            # once, right after a (re)start: a session cut short by the restart still gets its day volume back. Only
            # here: later on, a restore without a time belongs to a session that is being started (11-12 race fix).
            self._sleep_recovered = True
            if isinstance(r, dict) and r.get('at') is None and getattr(self, 'sleep_session', None) is None:
                r['at'] = _next_sleep_restore_at()
                _write_sleep_state(self._sleep_state)
        if isinstance(r, dict) and r.get('at') and time.time() >= float(r['at']) and not getattr(self, 'alarm_ringing', False):
            with self._lock:
                cur = self.volume
                busy = getattr(self, 'sleep_session', None) is not None
            if not busy:
                if type(r.get('sleep_volume')) is int and abs(cur - r['sleep_volume']) <= 1 and type(r.get('day_volume')) is int:
                    self.set_volume(r['day_volume'])
                    log.info('Sleep: day volume %d is back (was %d)', r['day_volume'], cur)
                else:
                    log.info('Sleep: volume was changed to %d meanwhile, leaving it', cur)
                self._sleep_state['restore'] = None
                _write_sleep_state(self._sleep_state)
        if isinstance(r, dict) and r.get('at') is None and getattr(self, 'sleep_session', None) is not None:
            with self._lock:
                cur = self.volume
                if self.sleep_session is not None:
                    self.sleep_session['volume'] = cur
            if r.get('sleep_volume') != cur:  # changed during the session (from the bed): the morning restore follows it
                r['sleep_volume'] = cur
                self._sleep_state['sleep_volume'] = cur
                _write_sleep_state(self._sleep_state)
        with self._lock:
            if getattr(self, 'sleep_session', None) is None:
                return
            sounding = bool(getattr(self, 'ambient_preset', None)) or (self.playing and not self.paused)
            if sounding:
                self._sleep_idle_since = None
                return
            if self._sleep_idle_since is None:
                self._sleep_idle_since = time.monotonic()
                return
            if time.monotonic() - self._sleep_idle_since < 8.0:
                return
            if self.sleep_mode == 'time':
                self._clear_sleep_locked()
        self._sleep_session_end('sound stopped')

    def current_track(self):
        with self._lock:
            if 0 <= self.index < len(self.queue):
                return self.queue[self.index]
            return None

    def status(self, lite: bool = False, qseq: int = None, lseq: int = None):
        with self._lock:
            t = self.queue[self.index] if (self.playing and 0 <= self.index < len(self.queue)) else None
            playing = self.playing
            paused = self.paused
            alarm_ringing = self.alarm_ringing
            elapsed = round(self.elapsed_base if playing else 0.0, 1)
            dur = self._safe_dur(t.get('duration')) if t else 0
            amb_pub = self._ambient_public_locked()
            ui_mono = getattr(self, '_open_ui_mono', 0.0) or 0.0
            ui_fresh = bool(self.open_ui_seq > 0 and ui_mono > 0.0 and 0.0 <= (time.monotonic() - ui_mono) < 10.0)
            if lite:
                return {
                    'playing': bool(playing and not paused and not amb_pub),
                    'paused': paused,
                    'alarm_ringing': alarm_ringing,
                    'boot_id': self.boot_id,
                    'open_ui_seq': self.open_ui_seq,
                    'open_ui_fresh': ui_fresh,
                    'elapsed': elapsed,
                    'duration': dur,
                    'volume': getattr(self, 'volume', 55),
                    'play_mode': getattr(self, 'play_mode', 'loop'),
                    'lyrics_seq': getattr(self, 'lyrics_seq', 1),
                    'ambient': amb_pub,
                    'sleep': self._sleep_public_locked(),
                    'sleep_session': self._sleep_session_public_locked(),
                    'sleep_preview': self._preview_public_locked(),
                    'track': {
                        'mid': t.get('mid', ''),
                        'file': t.get('url', '') if t.get('is_local') else '',
                        'title': t.get('title', ''),
                        'artist': t.get('artist', ''),
                        'cover': t.get('thumb') or t.get('cover', ''),
                    } if t else None,
                }
            ducked = self.ducked
            q_seq = self.queue_seq
            l_seq = self.lyrics_seq
            volume = self.volume
            index = self.index
            queue_len = len(self.queue)
            play_mode = self.play_mode
            sleep_pub = self._sleep_public_locked()
            sess_pub = self._sleep_session_public_locked()
            prev_pub = self._preview_public_locked()
            lyrics_snap = list(self.lyrics) if lseq != l_seq else None
            if qseq != q_seq:
                if self._cached_q_seq != q_seq:
                    self._cached_queue_pub = [
                        {
                            'mid': x.get('mid', ''),
                            'media_mid': x.get('media_mid', ''),
                            'title': x.get('title', ''),
                            'artist': x.get('artist', ''),
                            'album': x.get('album', ''),
                            'cover': x.get('cover', ''),
                            'thumb': x.get('thumb') or x.get('cover', ''),
                            'duration': x.get('duration', 0),
                            'vip': bool(x.get('vip')),
                            'source': x.get('source', ''),
                            'quality': x.get('quality', ''),
                            'url': x.get('url') if x.get('is_local') else None,
                            'is_local': bool(x.get('is_local')),
                        }
                        for x in self.queue[:200]
                    ]
                    self._cached_q_seq = q_seq
                queue_snap = self._cached_queue_pub
            else:
                queue_snap = None
        cfg = load_config()
        dur = self._safe_dur(t.get('duration')) if t else 0
        resp = {
            'playing': bool(playing and not paused and not amb_pub),
            'paused': paused,
            'ducked': ducked,
            'alarm_ringing': alarm_ringing,
            'boot_id': self.boot_id,
            'open_ui_seq': self.open_ui_seq,
            'open_ui_fresh': ui_fresh,
            'queue_seq': q_seq,
            'lyrics_seq': l_seq,
            'volume': volume,
            'elapsed': elapsed,
            'duration': dur,
            'track': {
                'mid': t.get('mid', ''),
                'file': t.get('url', '') if t.get('is_local') else '',
                'title': t.get('title', ''),
                'artist': t.get('artist', ''),
                'album': t.get('album', ''),
                'cover': t.get('cover', ''),
                'source': t.get('source', 'QQ音乐'),
                'quality': t.get('quality', ''),
                'duration': dur,
            } if t else None,
            'index': index,
            'queue_len': queue_len,
            'play_mode': play_mode,
            'ambient': amb_pub,
            'sleep': sleep_pub,
            'sleep_session': sess_pub,
            'sleep_preview': prev_pub,
            'user_profile': cfg.get('user_profile'),
            'quality_level': cfg.get('quality_level', 'lossless'),
            'max_cache_mb': cfg.get('max_cache_mb', 200),
        }
        if lyrics_snap is not None:
            resp['lyrics'] = lyrics_snap
        if queue_snap is not None:
            resp['queue'] = queue_snap
        return resp

    def play_tracks(self, tracks: list, start_index: int = 0, open_ui: bool = False):
        tracks = [x for x in tracks if isinstance(x, dict)] if isinstance(tracks, list) else []
        if not tracks:
            return {'ok': False, 'error': '没有可播放的歌曲'}
        try:
            idx = 0 if isinstance(start_index, bool) else int(start_index)
        except (TypeError, ValueError, OverflowError):
            idx = 0
        idx = max(0, min(idx, len(tracks) - 1))
        if len(tracks) > 200:
            start = max(0, min(idx - 100, len(tracks) - 200))
            tracks = tracks[start:start + 200]
            idx -= start
        with self._lock:
            self._clear_duck_except_home_locked()
            if getattr(self, 'ambient_preset', None):
                self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
            self.ambient_preset = None
            self.ambient_layer = None
            had_proc = self._stop_proc_locked()
            self.queue = list(tracks)
            self.queue_seq += 1
            self.index = idx
            self.playing = True
            self.paused = False
            self._fail_streak = 0
            self.elapsed_base = 0.0
            self.play_started_mono = 0.0
            self.lyrics = []
            self.lyrics_seq += 1
            self.just_started = True
            if open_ui:
                self.open_ui_seq += 1
                self._open_ui_mono = time.monotonic()
            gen = self._gen
        if had_proc:
            self._send_aec_flush()
        self._pause_bluetooth_if_streaming()
        self.save_session()
        threading.Thread(target=self._start_current_track, args=(0.0, gen), daemon=True).start()
        t = self.current_track() or {}
        return {
            'ok': True,
            'title': t.get('title', ''),
            'artist': t.get('artist', ''),
            'source': t.get('source', 'QQ音乐·SQ无损'),
            'quality': t.get('quality', 'SQ 无损 FLAC'),
            'count': len(tracks),
        }

    def play_query(self, query: str, source: str = 'auto'):
        if isinstance(query, (int, float)) and not isinstance(query, bool):
            query = str(query)
        raw_q = (query if isinstance(query, str) else '').strip()
        raw_clean = raw_q.lower().strip(' ，。！？,.!?《》"\'“”')
        if raw_clean in (
            'stop_sleep', '关闭睡眠', '关闭睡眠模式', '退出睡眠', '退出睡眠模式', '结束睡眠', '结束睡眠模式',
            '取消睡眠', '取消睡眠模式', '关闭助眠', '停止助眠', '关掉助眠', '不睡了', '起床了', '我醒了',
        ):
            res = self.sleep_session_stop()
            return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
        if raw_clean in ('关闭音乐', '关掉音乐', '停止播放', '暂停播放', '别唱了', '别放了', '不听了'):
            if getattr(self, 'sleep_session', None) is not None:
                res = self.sleep_session_stop()
                return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
            res = self.control('pause')
            return dict(res, mode_only=True, message=res.get('message') or '已暂停播放') if isinstance(res, dict) else res
        sleep_only_tails = {
            '睡眠', '睡眠模式', '助眠', '助眠模式', '助眠音乐', '睡眠音乐', '催眠曲', '摇篮曲', '睡前轻音乐', '钢琴摇篮曲', '睡前音乐',
        } | {n.lower() for _, _, _, n in SLEEP_COMBOS}
        for sp in ('关闭', '关掉', '停止', '取消', '停掉', '别放', '不要'):
            if raw_clean.startswith(sp):
                tail = raw_clean[len(sp):].strip()
                if tail in sleep_only_tails:
                    res = self.sleep_session_stop()
                    return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
                if tail in (
                    '环境音', '自然音', '自然环境音', '白噪音', '背景音', '助眠音', '音效',
                    '雨声', '细雨', '窗外细雨', '海浪', '海浪声', '海岸慢浪', '森林', '微风', '林间微风', '风声',
                    '溪流', '溪流声', '流水', '流水声', '山涧溪流', '虫鸣', '虫鸣声', '夏夜虫鸣', '褐噪', '褐噪音',
                    '柔和白噪音', '粉红噪音', '粉噪音', '低沉褐噪音', '棕噪音',
                    '轻琴', '雨夜轻琴', '助眠钢琴', '壁炉', '柴火', '柴火声', '壁炉柴火', '篝火',
                    '书房', '翻书声', '书房自习', '颂钵', '颂钵声', '禅意颂钵', '深度自习', '围炉夜读', '深度入眠',
                ):
                    if getattr(self, 'sleep_session', None) is not None:
                        res = self.sleep_session_stop()
                        return dict(res, mode_only=True, title='睡眠模式', source='睡眠模式', message=res.get('message') or '好的，已停止')
                    res = self.set_ambient('off')
                    return (
                        dict(res, mode_only=True, title='自然环境音', source='自然环境音', message=res.get('message') or '已关闭环境音')
                        if isinstance(res, dict) and 'ambient' in res else res
                    )
        matched_mode = None
        if '单曲循环' in raw_q:
            matched_mode = 'single'
        elif '随机播放' in raw_q:
            matched_mode = 'shuffle'
        elif '列表循环' in raw_q or '顺序播放' in raw_q:
            matched_mode = 'loop'
        if matched_mode:
            self.set_play_mode(matched_mode)
        q = raw_q
        for kw in ('单曲循环', '随机播放', '列表循环', '顺序播放'):
            q = q.replace(kw, '').strip()
        for p in (
            '请帮我', '帮我', '用qq音乐', '用QQ音乐', '在QQ音乐',
            '切换为', '切换成', '切成', '设为', '设置成', '设置', '改成', '改为',
            '开启', '打开', '播放', '放一首', '放首', '来一首', '来首',
            '放点', '来点', '播点', '放个', '来个', '听点', '我想听', '听一下', '听听',
        ):
            if q.lower().startswith(p.lower()):
                q = q[len(p):].strip()
        if matched_mode and q in ('', '模式', '播放模式'):
            with self._lock:
                has_q = bool(self.queue)
            if has_q:
                res = self.control(matched_mode)
                t = self.current_track() or {}
                return dict(res, mode_only=True, title=t.get('title', ''), artist=t.get('artist', ''))
            q = ''

        raw_ql = raw_q.lower()
        explicit_song = any(
            k in raw_ql
            for k in ('一首', '这首', '那首', '首歌', '歌曲', '单曲', '原唱', '翻唱', 'live', 'mv', '专辑', '歌单', '我喜欢', '收藏', '热歌', '新歌', '飙升')
        )
        amb_q = q.lower().strip(' ，。！？,.!?《》"\'“”')
        had_amb_suf = False
        for suf in ('环境音', '自然音', '白噪音', '背景音', '助眠音', '音效', '声音', '配方', '助眠'):
            if amb_q.endswith(suf) and len(amb_q) > len(suf):
                amb_q = amb_q[:-len(suf)].strip()
                had_amb_suf = True
        for pre in ('一点', '一段', '一些', '点', '些'):
            if amb_q.startswith(pre) and len(amb_q) > len(pre):
                amb_q = amb_q[len(pre):].strip()
        amb_exact_map = {
            '深度自习': 'deep_study', '沉浸自习': 'deep_study',
            '围炉夜读': 'cozy_read', '阅读放空': 'cozy_read',
            '深度入眠': 'deep_sleep', '深度助眠': 'deep_sleep',
            '雨夜轻琴': 'sleep_piano', '助眠钢琴': 'sleep_piano', '睡眠钢琴': 'sleep_piano',
            '壁炉柴火': 'fireplace', '壁炉': 'fireplace', '壁炉声': 'fireplace', '柴火': 'fireplace', '柴火声': 'fireplace', '篝火': 'fireplace', '篝火声': 'fireplace',
            '书房自习': 'library', '图书馆': 'library', '翻书声': 'library', '自习室': 'library',
            '禅意颂钵': 'singing_bowl', '颂钵': 'singing_bowl', '颂钵声': 'singing_bowl', '冥想音': 'singing_bowl',
            '窗外细雨': 'rain', '雨声': 'rain', '细雨': 'rain', '下雨声': 'rain', '白噪音': 'white_noise',
            '粉红噪音': 'pink_noise', '粉噪音': 'pink_noise', '低沉褐噪音': 'brown_noise', '棕噪音': 'brown_noise',
            '海岸慢浪': 'waves', '海浪': 'waves', '海浪声': 'waves', '潮汐声': 'waves',
            '森林微风': 'forest', '林间微风': 'forest', '山谷微风': 'forest', '微风声': 'forest', '风声': 'forest',
            '山涧溪流': 'stream', '潺潺流水': 'stream', '溪流': 'stream', '溪流声': 'stream', '流水声': 'stream',
            '夏夜虫鸣': 'night', '虫鸣': 'night', '虫鸣声': 'night', '静心褐噪': 'brown_noise', '褐噪': 'brown_noise', '褐噪音': 'brown_noise',
        }
        amb_suf_only = {'潮汐': 'waves', '翻书': 'library', '森林': 'forest', '流水': 'stream', '大海': 'waves', '冥想': 'singing_bowl', '柔和': 'white_noise'}

        def _wrap_amb_res(res):
            if isinstance(res, dict) and res.get('ok') and 'ambient' in res:
                amb_obj = res.get('ambient') or {}
                nm = amb_obj.get('name') or ''
                return dict(
                    res,
                    mode_only=True,
                    title=nm,
                    source='自然环境音',
                    message=res.get('message') or (f'已为您开启自然环境音：{nm}' if nm else '已开启自然环境音'),
                )
            return res

        if not explicit_song:
            if amb_q in amb_exact_map:
                return _wrap_amb_res(self.set_ambient(amb_exact_map[amb_q]))
            if had_amb_suf and amb_q in amb_suf_only:
                return _wrap_amb_res(self.set_ambient(amb_suf_only[amb_q]))
        if 'u盘' in raw_ql or '本地' in raw_ql:
            lq = re.sub(r'^(?:里的|里|的)+', '', re.sub(r'(?i)u盘|本地', '', q).strip()).strip()
            if lq.lower() in ('歌', '音乐', '歌曲', '随便', '随机', '好听的歌', 'random'):
                lq = ''
            loc = search_local(lq)
            if loc:
                if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                    random.shuffle(loc)
                return self.play_tracks(loc, 0)

        if any(k in raw_ql for k in ('我喜欢', '收藏', '我的歌单', 'kpop', '好听', '纯音乐', '娜娜', '最近爱听', '歌单')):
            pls = qq_user_playlists()
            for pl in pls:
                clean_name = pl['name'].replace('❤️', '').replace('🎵', '').strip().lower()
                if clean_name and clean_name in raw_ql:
                    tracks = qq_playlist_tracks(pl['id'])
                    if tracks:
                        if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                            random.shuffle(tracks)
                        return self.play_tracks(tracks, 0)
            if pls and any(k in raw_ql for k in ('我喜欢', '收藏', '我的歌单')):
                tracks = qq_playlist_tracks(pls[0]['id'])
                if tracks:
                    if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                        random.shuffle(tracks)
                    return self.play_tracks(tracks, 0)

        toplist_map = {
            '热歌': 26, '热歌榜': 26,
            '飙升': 62, '飙升榜': 62,
            '新歌': 27, '新歌榜': 27,
            '流行指数': 4, '流行指数榜': 4, '流行榜': 4,
            '内地': 5, '内地榜': 5,
            '香港地区': 59, '香港地区榜': 59, '香港榜': 59, '港歌榜': 59,
            '台湾地区': 61, '台湾地区榜': 61, '台湾榜': 61,
            '欧美': 3, '欧美榜': 3,
            '韩国': 16, '韩国榜': 16,
            '日本': 17, '日本榜': 17,
        }
        top_id = toplist_map.get(q)
        if top_id is None and (not q or q.lower() in ('歌', '音乐', '歌曲', '随便', '随机', '好听的歌', 'random')):
            top_id = 26
        if top_id is not None:
            tracks = qq_toplist(top_id, limit=35)
            if tracks:
                if '随机' in raw_q or '随便' in raw_q or 'random' in raw_ql:
                    random.shuffle(tracks)
                return self.play_tracks(tracks, 0)

        tracks = qq_search(q, limit=20)
        if not tracks:
            return {'ok': False, 'error': f'未在 QQ 音乐找到「{q}」'}
        return self.play_tracks(tracks, 0)

    def seek(self, sec: float):
        if isinstance(sec, bool):
            return {'ok': False}
        try:
            sec_f = float(sec)
        except (TypeError, ValueError):
            return {'ok': False}
        if not math.isfinite(sec_f):
            return {'ok': False}
        with self._lock:
            if not (0 <= self.index < len(self.queue)):
                return {'ok': False}
            target_sec = max(0.0, min(sec_f, 86400.0))
            dur = self._safe_dur(self.queue[self.index].get('duration'))
            if dur > 1 and target_sec >= dur - 1:
                target_sec = float(dur - 1)
            self._clear_duck_except_home_locked()
            if getattr(self, 'ambient_preset', None):
                self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
            self.ambient_preset = None
            self.ambient_layer = None
            had_proc = self._stop_proc_locked()
            if not self.playing:
                self.playing = True
                self.paused = True
            was_paused = self.paused
            same_pos = was_paused and not had_proc and abs(target_sec - self.elapsed_base) < 0.05
            self._fail_streak = 0
            self.elapsed_base = target_sec
            gen = self._gen
        if had_proc:
            self._send_aec_flush()
        if not was_paused:
            threading.Thread(target=self._start_current_track, args=(target_sec, gen), daemon=True).start()
        if not same_pos:
            self.save_session()
        return {'ok': True, 'elapsed': target_sec}

    def control(self, action: str, index: int = None, seconds: float = None, source: str = None):
        action = (action if isinstance(action, str) else '').lower().strip()
        mode_labels = {'loop': '列表循环', 'shuffle': '随机播放', 'single': '单曲循环'}
        if action in ('mode', 'toggle_mode', '切换模式', 'loop', '列表循环', '顺序播放', 'single', 'repeat_one', '单曲循环', 'shuffle', 'random', '随机', '随机播放'):
            with self._lock:
                if action in ('mode', 'toggle_mode', '切换模式'):
                    cycle = {'loop': 'shuffle', 'shuffle': 'single', 'single': 'loop'}
                    new_m = cycle.get(self.play_mode, 'loop')
                elif action in ('single', 'repeat_one', '单曲循环'):
                    new_m = 'single'
                elif action in ('shuffle', 'random', '随机', '随机播放'):
                    new_m = 'shuffle'
                else:
                    new_m = 'loop'
                self.play_mode = new_m
                if self.playing and not self.paused:
                    self.just_started = True
            self.set_play_mode(new_m)
            return {'ok': True, 'play_mode': new_m, 'message': f'已切换为{mode_labels[new_m]}'}
        if action in ('open', 'open_ui', '打开', '打开音乐'):
            with self._lock:
                self.just_started = True
                self.open_ui_seq += 1
                self._open_ui_mono = time.monotonic()
            return {'ok': True, 'state': 'opened', 'message': '已打开QQ音乐界面'}
        if action in ('duck', 'unduck'):
            do_duck_flush = False
            active = False
            src_key = str(source).strip().lower()[:16] if isinstance(source, str) and source.strip() else 'study'
            with self._lock:
                now_m = time.monotonic()
                leases = getattr(self, '_duck_leases', None)
                if not isinstance(leases, dict):
                    leases = {}
                    self._duck_leases = leases
                for k in [k for k, v in leases.items() if v <= now_m]:
                    leases.pop(k, None)
                active = bool(getattr(self, 'ambient_preset', None) or (self.playing and not self.paused))
                if action == 'duck':
                    dur_s = 18.0
                    if seconds is not None and not isinstance(seconds, bool):
                        with contextlib.suppress(TypeError, ValueError, OverflowError):
                            sf = float(seconds)
                            if math.isfinite(sf):
                                dur_s = max(3.0, min(65.0, sf))
                    was_study_duck = now_m < getattr(self, 'study_duck_until', 0.0)
                    leases[src_key] = max(leases.get(src_key, 0.0), now_m + dur_s)
                    self.study_duck_until = max(getattr(self, 'study_duck_until', 0.0), max(leases.values(), default=0.0))
                    do_duck_flush = not was_study_duck and active
                else:
                    leases.pop(src_key, None)
                    self.study_duck_until = max((v for v in leases.values() if v > now_m), default=0.0)
            if do_duck_flush:
                self._send_aec_flush()
            return {'ok': True, 'state': 'ducked' if action == 'duck' else 'unducked', 'active': active}
        end_sleep_sess = False
        with self._lock:
            resp = None
            flush_aec = False
            save_needed = True
            if action in (
                'stop', '停止', '关闭', 'jump', 'next', '下一首', '换一首', 'prev', '上一首',
                'pause', '暂停', 'resume', 'play', '继续',
            ):
                self._clear_duck_except_home_locked()
                if getattr(self, 'ambient_preset', None):
                    self.ambient_seq = getattr(self, 'ambient_seq', 0) + 1
            if action in ('stop', '停止', '关闭'):
                if getattr(self, 'sleep_session', None) is not None:
                    end_sleep_sess = True
                    self._clear_sleep_locked()
                self.ambient_preset = None
                self.ambient_layer = None
                self.just_started = False
                self.playing = False
                self.paused = False
                self._fail_streak = 0
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                if self.lyrics:
                    self.lyrics = []
                    self.lyrics_seq += 1
                flush_aec = self._stop_proc_locked()
                resp = {'ok': True, 'state': 'stopped', 'message': '已停止播放'}
            elif action in ('pause', '暂停'):
                self.just_started = False
                was_amb = bool(getattr(self, 'ambient_preset', None))
                self.ambient_preset = None
                self.ambient_layer = None
                was_active = bool(self.playing and not self.paused)
                if was_active:
                    self.paused = True
                if was_amb:
                    self._stop_proc_locked()
                flush_aec = was_active or was_amb
                save_needed = was_active or was_amb
                resp = {'ok': True, 'state': 'paused', 'message': '已暂停'}
            elif action in ('resume', 'play', '继续'):
                if self.queue:
                    was_amb = bool(getattr(self, 'ambient_preset', None))
                    self.ambient_preset = None
                    self.ambient_layer = None
                    was_paused = not self.playing or self.paused or was_amb
                    self.just_started = True
                    self.playing = True
                    self.paused = False
                    self._fail_streak = 0
                    if was_amb or (self._proc is None and (was_paused or self._starting_gen != self._gen)):
                        flush_aec = self._stop_proc_locked()
                        gen = self._gen
                        threading.Thread(target=self._start_current_track, args=(self.elapsed_base, gen), daemon=True).start()
                    resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
                else:
                    return {'ok': False, 'error': '当前没有暂停的音乐'}
            elif action == 'jump' and index is not None:
                if isinstance(index, bool):
                    return {'ok': False}
                try:
                    j_idx = int(index)
                except (TypeError, ValueError, OverflowError):
                    return {'ok': False}
                if 0 <= j_idx < len(self.queue):
                    self.ambient_preset = None
                    self.ambient_layer = None
                    flush_aec = self._stop_proc_locked()
                    prev_idx = self.index
                    self.just_started = True
                    self.index = j_idx
                    self.playing = True
                    self.paused = False
                    self._fail_streak = 0
                    self.elapsed_base = 0.0
                    self.play_started_mono = 0.0
                    if self.index != prev_idx:
                        self.lyrics = []
                        self.lyrics_seq += 1
                    gen = self._gen
                    threading.Thread(target=self._start_current_track, args=(0.0, gen), daemon=True).start()
                    resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
                else:
                    return {'ok': False}
            elif action in ('next', '下一首', '换一首'):
                if not self.queue:
                    return {'ok': False, 'error': '播放列表为空'}
                self.ambient_preset = None
                self.ambient_layer = None
                flush_aec = self._stop_proc_locked()
                prev_idx = self.index
                if self.play_mode == 'shuffle' and len(self.queue) > 1:
                    self.index = (self.index + random.randrange(1, len(self.queue))) % len(self.queue)
                else:
                    self.index = (self.index + 1) % len(self.queue)
                self.just_started = True
                self.playing = True
                self.paused = False
                self._fail_streak = 0
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                if self.index != prev_idx:
                    self.lyrics = []
                    self.lyrics_seq += 1
                gen = self._gen
                threading.Thread(target=self._start_current_track, args=(0.0, gen), daemon=True).start()
                resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
            elif action in ('prev', '上一首'):
                if not self.queue:
                    return {'ok': False, 'error': '播放列表为空'}
                self.ambient_preset = None
                self.ambient_layer = None
                flush_aec = self._stop_proc_locked()
                prev_idx = self.index
                self.index = (self.index - 1) % len(self.queue)
                self.just_started = True
                self.playing = True
                self.paused = False
                self._fail_streak = 0
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                if self.index != prev_idx:
                    self.lyrics = []
                    self.lyrics_seq += 1
                gen = self._gen
                threading.Thread(target=self._start_current_track, args=(0.0, gen), daemon=True).start()
                resp = {'ok': True, 'state': 'playing', 'track': self._public_track(self.queue[self.index])}
        if resp is not None:
            if flush_aec:
                self._send_aec_flush()
            if end_sleep_sess:
                self._sleep_session_end('stop')
            if resp.get('state') == 'playing':
                self._pause_bluetooth_if_streaming()
            if save_needed:
                self.save_session()
            return resp
        return {'ok': False, 'error': f'未知指令: {action}'}

    def _stop_proc_locked(self) -> bool:
        self._gen += 1
        p2 = getattr(self, '_proc2', None)
        had_proc = self._proc is not None or p2 is not None or self._pacat is not None
        for p in (self._proc, p2, self._pacat):
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
        self._proc = None
        self._proc2 = None
        self._pacat = None
        return had_proc

    def _start_current_track(self, seek_sec: float = 0.0, expected_gen: int = None):
        with self._lock:
            if expected_gen is not None and expected_gen != self._gen:
                return
            had_proc = self._stop_proc_locked()
            can_start = bool(self.playing and not self.paused and (0 <= self.index < len(self.queue)))
            if can_start:
                gen = self._gen
                self._starting_gen = gen
                track = dict(self.queue[self.index])
                self.elapsed_base = float(seek_sec)
                self.play_started_mono = 0.0
                need_lrc = not self.lyrics
        if had_proc:
            self._send_aec_flush()
        if not can_start:
            return

        # Fetch synchronized lyrics in background
        if need_lrc and track.get('mid'):
            def _load_lrc(m, g):
                lrc = qq_lyric(m)
                changed = False
                with self._lock:
                    if g == self._gen and lrc != self.lyrics:
                        self.lyrics = lrc
                        self.lyrics_seq += 1
                        changed = True
                if changed:
                    self.save_session()
            threading.Thread(target=_load_lrc, args=(track['mid'], gen), daemon=True).start()

        # Refresh signed remote URL if missing or older than 1 hour (vkey expiry)
        if not track.get('is_local') and track.get('url'):
            try:
                url_expired = track.get('_url_ts') is None or time.monotonic() - float(track['_url_ts']) > 3600
            except (TypeError, ValueError):
                url_expired = True
            if url_expired:
                track['url'] = None

        # Probe duration once for local/USB files so UI progress & seek work accurately
        if track.get('is_local') and track.get('url') and not _is_allowed_local_audio(track['url']):
            log.warning('Refusing local audio outside allowed directories: %r', track['url'])
            with self._lock:
                if self._starting_gen == gen:
                    self._starting_gen = -1
            self._advance_on_eof(gen, force_next=True)
            return
        if track.get('is_local') and track.get('url') and not track.get('duration'):
            try:
                out = subprocess.check_output(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', '--', track['url']],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2.5,
                ).strip()
                dur_f = float(out)
                if math.isfinite(dur_f) and dur_f > 0:
                    track['duration'] = max(1, int(round(dur_f)))
                    with self._lock:
                        if gen == self._gen and 0 <= self.index < len(self.queue):
                            self.queue[self.index] = track
                            self.queue_seq += 1
                            self._cached_q_seq = 0
                    self.save_session()
            except Exception:
                pass

        # Resolve stream URL if needed
        if not track.get('url'):
            if track.get('mid'):
                vk = resolve_qq_vkey(track['mid'], track.get('media_mid', ''))
                if vk and vk.get('url'):
                    track.update(vk)
            if not track.get('url'):
                q = f"{track.get('artist', '')} {track.get('title', '')}".strip()
                fb = resolve_hires_fallback(q, title_hint=track.get('title', ''), artist_hint=track.get('artist', ''))
                if fb and fb.get('url'):
                    track.update(fb)

            meta_changed = False
            with self._lock:
                if gen == self._gen and 0 <= self.index < len(self.queue):
                    prev_tr = self.queue[self.index]
                    if (
                        prev_tr.get('duration') != track.get('duration')
                        or prev_tr.get('quality') != track.get('quality')
                        or prev_tr.get('source') != track.get('source')
                    ):
                        self.queue_seq += 1
                        meta_changed = True
                    self.queue[self.index] = track
                    self._cached_q_seq = 0
            if meta_changed:
                self.save_session()

        with self._lock:
            if gen != self._gen or not self.playing or self.paused:
                if self._starting_gen == gen:
                    self._starting_gen = -1
                return

        if not track.get('url'):
            with self._lock:
                if self._starting_gen == gen:
                    self._starting_gen = -1
            self._advance_on_eof(gen, force_next=True)
            return

        threading.Thread(target=self._decode_worker, args=(gen, track, seek_sec), daemon=True).start()

    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        chunk = stream.read(n)
        if not chunk or len(chunk) == n:
            return chunk or b''
        buf = bytearray(chunk)
        while len(buf) < n:
            more = stream.read(n - len(buf))
            if not more:
                break
            buf.extend(more)
        return bytes(buf)

    @staticmethod
    def _readinto_exact(stream, mv: memoryview) -> int:
        n = len(mv)
        got = stream.readinto(mv)
        if not got or got == n:
            return got or 0
        while got < n:
            more = stream.readinto(mv[got:])
            if not more:
                break
            got += more
        return got

    def _decode_worker(self, gen: int, track: dict, seek_sec: float):
        url = track['url']
        is_local = bool(track.get('is_local'))
        if is_local:
            if not _is_allowed_local_audio(url):
                with self._lock:
                    if self._starting_gen == gen:
                        self._starting_gen = -1
                log.warning('Blocked disallowed local path in decoder: %r', url)
                self._advance_on_eof(gen, force_next=True)
                return
        else:
            orig_url = url
            resolved_url = _resolve_safe_ffmpeg_url(url, track.get('headers'))
            if not resolved_url:
                with self._lock:
                    if self._starting_gen == gen:
                        self._starting_gen = -1
                log.warning('Blocked unsafe stream URL in decoder: %r', url)
                self._advance_on_eof(gen, force_next=True)
                return
            url = resolved_url
        cmd = ['ffmpeg', '-loglevel', 'error', '-nostdin']
        if seek_sec > 0:
            cmd.extend(['-ss', f'{seek_sec:.1f}'])
        if is_local:
            cmd.extend(['-protocol_whitelist', 'file,pipe'])
        else:
            hdrs = track.get('headers') or {}
            if hdrs:
                same_root = _cookie_root(urllib.parse.urlsplit(orig_url).hostname or '') == _cookie_root(urllib.parse.urlsplit(url).hostname or '')
                hdr_str = ''.join(
                    f'{str(k).translate({10: None, 13: None})}: {str(v).translate({10: None, 13: None})}\r\n'
                    for k, v in hdrs.items()
                    if str(k).translate({10: None, 13: None}) and (same_root or str(k).strip().lower() not in ('cookie', 'authorization'))
                )
                if hdr_str:
                    cmd.extend(['-headers', hdr_str])
            cmd.extend([
                '-rw_timeout', '15000000',
                '-protocol_whitelist', 'http,https,tcp,tls,crypto',
                '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '3',
            ])
        # High-precision conversion to the existing 48kHz float stereo pipeline.
        # Do not force resampling when the source is already 48kHz.
        cmd.extend(['-i', url, '-af', f'aresample={OUT_RATE}:resampler=soxr:precision=28',
                    '-f', 'f32le', '-ac', '2', '-ar', str(OUT_RATE), 'pipe:1'])

        env = {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000', 'PULSE_SERVER': 'unix:/run/user/1000/pulse/native'}
        pacat = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            with self._lock:
                if self._starting_gen == gen:
                    self._starting_gen = -1
            log.error('Audio pipeline start failed: %s', e)
            self._advance_on_eof(gen, force_next=True)
            return

        with self._lock:
            if self._starting_gen == gen:
                self._starting_gen = -1
            if gen != self._gen:
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
                if proc.stdout:
                    proc.stdout.close()
                return
            self._proc = proc
            self._pacat = None

        block_bytes = 960 * 2 * 4  # 20ms of 48kHz stereo float32 (7680 bytes)
        pcm_buf = bytearray(block_bytes)
        pcm_mv = memoryview(pcm_buf)
        got_any = False
        pause_started = None
        paused_idle_release = False
        fade_prev = None
        pacat_retry_mono = 0.0
        try:
            while True:
                with self._lock:
                    if gen != self._gen or not self.playing:
                        break
                    user_paused = self.paused
                    ducked = self.ducked
                    study_duck = time.monotonic() < getattr(self, 'study_duck_until', 0.0)
                    is_paused = user_paused or ducked or study_duck
                    if user_paused:
                        if pause_started is None:
                            pause_started = time.monotonic()
                        elif time.monotonic() - pause_started >= 45.0:
                            # Release idle ffmpeg (~44MB RAM) after 45s of pause; resume will seek cleanly
                            paused_idle_release = True
                            if self._proc is proc:
                                self._proc = None
                                self._pacat = None
                            log.info('Paused >45s -> releasing idle ffmpeg decoder (resume at %.1fs)', self.elapsed_base)
                            break
                    else:
                        pause_started = None

                if is_paused:
                    time.sleep(0.04)
                    continue

                pause_started = None
                got = self._readinto_exact(proc.stdout, pcm_mv)
                if not got:
                    break
                if got < block_bytes:
                    # Pad final partial block to exact 7680-byte frame boundary
                    aligned_len = (got // 8) * 8
                    pcm_mv[aligned_len:] = b'\x00' * (block_bytes - aligned_len)

                got_any = True

                # Round 109 sleep timer fade-out (no-op unless a fade is due)
                with self._lock:
                    fade_target = self._sleep_gain_locked()
                if fade_prev is None:
                    fade_prev = fade_target
                if fade_target < 0.9999 or fade_prev < 0.9999:
                    self._apply_gain(pcm_buf, fade_prev, fade_target)
                fade_prev = fade_target

                # Send PCM to our non-invasive AEC bridge inside thinksmart-client (serialized against flush)
                sent_to_aec = False
                with self._aec_lock:
                    for sock in list(self.aec_clients):
                        try:
                            sock.sendall(pcm_mv)
                            sent_to_aec = True
                        except Exception:
                            self.aec_clients.discard(sock)
                            try:
                                sock.close()
                            except Exception:
                                pass

                if not sent_to_aec:
                    try:
                        if pacat is None and time.monotonic() >= pacat_retry_mono:
                            pacat = subprocess.Popen(
                                ['pacat', '--rate=48000', '--channels=2', '--format=float32le', '--latency-msec=60'],
                                stdin=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                env=env,
                            )
                            with self._lock:
                                if gen != self._gen or not self.playing:
                                    break
                                self._pacat = pacat
                        if pacat is not None:
                            pacat.stdin.write(pcm_mv)
                            pacat.stdin.flush()
                    except OSError:
                        pacat_retry_mono = time.monotonic() + 0.3
                        if pacat is not None:
                            try:
                                pacat.kill()
                                pacat.wait(timeout=1)
                            except Exception:
                                pass
                            if pacat.stdin:
                                try:
                                    pacat.stdin.close()
                                except Exception:
                                    pass
                            pacat = None
                        with self._lock:
                            if self._pacat is not None and gen == self._gen:
                                self._pacat = None
                    if pacat is None:
                        time.sleep(0.02)
                elif pacat is not None:
                    try:
                        pacat.kill()
                        pacat.wait(timeout=1)
                    except Exception:
                        pass
                    if pacat.stdin:
                        try:
                            pacat.stdin.close()
                        except Exception:
                            pass
                    pacat = None
                    with self._lock:
                        if self._pacat is not None and gen == self._gen:
                            self._pacat = None

                with self._lock:
                    if gen == self._gen and not self.paused and not self.ducked and time.monotonic() >= getattr(self, 'study_duck_until', 0.0):
                        self.elapsed_base += 0.02
        except Exception:
            pass
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._pacat = None
            for p in (proc, pacat):
                if p is not None:
                    try:
                        p.kill()
                        p.wait(timeout=1)
                    except Exception:
                        pass
                    for s in (p.stdin, p.stdout, p.stderr):
                        if s is not None:
                            try:
                                s.close()
                            except Exception:
                                pass

        with self._lock:
            if gen != self._gen or not self.playing:
                return
            still_paused = self.paused
            cur_elapsed = self.elapsed_base

        if still_paused:
            if paused_idle_release:
                self.save_session()
            return
        if paused_idle_release:
            self._start_current_track(cur_elapsed, gen)
            return

        if got_any:
            dur = self._safe_dur(track.get('duration'))
            drop_retries = self._safe_dur(track.get('_drop_retries'))
            if not track.get('is_local') and dur > 30 and cur_elapsed < (dur - 8) and drop_retries < 2:
                log.warning('Stream EOF early at %.1fs / %ds -> reconnecting at %.1fs', cur_elapsed, dur, cur_elapsed)
                track['_drop_retries'] = drop_retries + 1
                try:
                    url_stale = track.get('_url_ts') is None or time.monotonic() - float(track['_url_ts']) > 300
                except (TypeError, ValueError):
                    url_stale = True
                if url_stale:
                    track['url'] = None
                with self._lock:
                    if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                        self.queue[self.index] = track
                        self._cached_q_seq = 0
                self._start_current_track(cur_elapsed, gen)
                return
            track['_drop_retries'] = 0
            with self._lock:
                self._fail_streak = 0
                if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                    self.queue[self.index]['_drop_retries'] = 0
            self._advance_on_eof(gen, force_next=False)
        else:
            dur = self._safe_dur(track.get('duration'))
            if seek_sec > 0 and dur > 0 and seek_sec >= max(1, dur - 3):
                self._advance_on_eof(gen, force_next=False)
                return
            if not track.get('_retried') and not track.get('is_local'):
                had_drop_retries = int(track.get('_drop_retries') or 0) > 0
                track['_retried'] = True
                track['_drop_retries'] = 0
                q = f"{track.get('artist', '')} {track.get('title', '')}".strip()
                fb = resolve_hires_fallback(q, title_hint=track.get('title', ''), artist_hint=track.get('artist', ''))
                if fb and fb.get('url'):
                    track.update(fb)
                    fb_seek = 0.0 if had_drop_retries else seek_sec
                    meta_changed = False
                    with self._lock:
                        if gen == self._gen and self.playing and 0 <= self.index < len(self.queue):
                            prev_tr = self.queue[self.index]
                            if (
                                prev_tr.get('duration') != track.get('duration')
                                or prev_tr.get('quality') != track.get('quality')
                                or prev_tr.get('source') != track.get('source')
                            ):
                                self.queue_seq += 1
                                meta_changed = True
                            self.queue[self.index] = track
                            self._cached_q_seq = 0
                    if meta_changed:
                        self.save_session()
                    self._start_current_track(fb_seek, gen)
                    return
            self._advance_on_eof(gen, force_next=True)

    def _advance_on_eof(self, gen: int, force_next: bool = False):
        with self._lock:
            if gen != self._gen or not self.playing or self.paused or not self.queue:
                return
            self._gen += 1
            next_gen = self._gen
            paused_on_fail = False
            sleep_stop = False
            if not force_next and self.sleep_mode == 'tracks':
                self.sleep_tracks_left -= 1
                if self.sleep_tracks_left <= 0:
                    sleep_stop = True
            if force_next:
                self._fail_streak += 1
                if self._fail_streak >= min(max(len(self.queue), 1), 5):
                    log.warning('Consecutive track failures (%d) -> pausing playback to prevent spin loop', self._fail_streak)
                    self.paused = True
                    self._fail_streak = 0
                    paused_on_fail = True
            if not paused_on_fail:
                prev_idx = self.index
                if self.play_mode == 'single' and not force_next:
                    pass  # Replay current self.index
                elif self.play_mode == 'shuffle' and len(self.queue) > 1:
                    self.index = (self.index + random.randrange(1, len(self.queue))) % len(self.queue)
                else:
                    self.index = (self.index + 1) % len(self.queue)
                self.elapsed_base = 0.0
                self.play_started_mono = 0.0
                if self.index != prev_idx:
                    self.lyrics = []
                    self.lyrics_seq += 1
                if sleep_stop:
                    # Sleep timer done: rest on the next track, paused at 0:00 (resume starts it)
                    self.paused = True
                    self.just_started = False
                    self._clear_sleep_locked()
        if sleep_stop and not paused_on_fail:
            self._send_aec_flush()
        self.save_session()
        if sleep_stop and not paused_on_fail:
            log.info('Sleep timer: last track finished -> paused')
            return
        if not paused_on_fail:
            self._start_current_track(0.0, next_gen)


engine = QQMusicEngine()


def start_aec_unix_server():
    """Accept connections from /usr/local/lib/thinksmart-aec-bridge/sitecustomize.py."""
    srv = None
    try:
        if os.path.lexists(AEC_SOCK_PATH):
            st_sock = os.lstat(AEC_SOCK_PATH)
            if not stat.S_ISSOCK(st_sock.st_mode):
                raise OSError(f'Refusing to unlink non-socket AEC path: {AEC_SOCK_PATH}')
            os.unlink(AEC_SOCK_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            srv.bind(AEC_SOCK_PATH)
        finally:
            os.umask(old_umask)
        os.chmod(AEC_SOCK_PATH, 0o600)
        srv.listen(4)
        def _accept_loop():
            while True:
                try:
                    conn, _ = srv.accept()
                    try:
                        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
                        conn.settimeout(0.25)
                    except Exception:
                        pass
                    with engine._aec_lock:
                        for old in list(engine.aec_clients):
                            try:
                                if old.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b'':
                                    engine.aec_clients.discard(old)
                                    old.close()
                            except (BlockingIOError, InterruptedError):
                                pass
                            except OSError:
                                engine.aec_clients.discard(old)
                                try:
                                    old.close()
                                except Exception:
                                    pass
                        while len(engine.aec_clients) >= 2:
                            evict = next(iter(engine.aec_clients))
                            engine.aec_clients.discard(evict)
                            with contextlib.suppress(Exception):
                                evict.close()
                        engine.aec_clients.add(conn)
                    log.info('AEC bridge client connected')
                except OSError:
                    if srv.fileno() < 0:
                        break
                    time.sleep(1)
                except Exception:
                    time.sleep(1)
        threading.Thread(target=_accept_loop, daemon=True).start()
        return srv
    except Exception as e:
        if srv is not None:
            with contextlib.suppress(Exception):
                srv.close()
        log.warning('AEC socket setup warning: %s', e)
        return None


async def watch_xiaozhi_events():
    """Watch XiaoZhi's SSE stream (127.0.0.1:8090/events) externally without modifying XiaoZhi."""
    import aiohttp
    prev_state = 'idle'
    voice_busy = False
    alarm_ringing = False
    prev_bt = None

    def _sync_bt(bt_obj) -> bool:
        nonlocal prev_bt
        if not isinstance(bt_obj, dict):
            return False
        now_bt = any(
            isinstance(d, dict) and d.get('streaming')
            for d in (bt_obj.get('devices') or [])
        )
        with engine._lock:
            engine.bt_streaming = now_bt
            should_pause = (
                prev_bt is False
                and now_bt
                and ((engine.playing and not engine.paused) or bool(getattr(engine, 'ambient_preset', None)))
                and (time.monotonic() - engine._bt_pause_sent_mono) > 5.0
            )
        prev_bt = now_bt
        return bool(should_pause)

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=35)
    last_err_log = 0.0
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False, read_bufsize=2 * 1024 * 1024) as sess:
        while True:
            try:
                async with sess.get('http://127.0.0.1:8090/events') as resp:
                    resp.raise_for_status()
                    event_type = ''
                    async for raw_line in resp.content:
                        line = raw_line.decode('utf-8', errors='replace').strip()
                        if not line:
                            event_type = ''
                            continue
                        if line.startswith('event:'):
                            event_type = line[6:].strip()
                        elif line.startswith('data:'):
                            data_str = line[5:].strip()
                            try:
                                data = json.loads(data_str)
                            except Exception:
                                continue
                            if not isinstance(data, dict):
                                continue

                            if event_type == 'volume' and data.get('volume') is not None and not data.get('ring') and not isinstance(data.get('volume'), bool):
                                try:
                                    with engine._lock:
                                        if data.get('source') == 'page' and (time.monotonic() - engine._local_vol_mono) < 1.0:
                                            pass
                                        else:
                                            engine.volume = max(0, min(100, int(float(data['volume']))))
                                except (TypeError, ValueError, OverflowError):
                                    pass
                            elif event_type == 'alarms':
                                alarm_ringing = bool(data.get('ringing'))
                                with engine._lock:
                                    engine.alarm_ringing = alarm_ringing
                                    has_sleep_sess = getattr(engine, 'sleep_session', None) is not None
                                engine.set_ducked(voice_busy or alarm_ringing)
                                if alarm_ringing and has_sleep_sess:
                                    await asyncio.get_running_loop().run_in_executor(None, engine.on_alarm_ringing)
                            elif event_type == 'bluetooth':
                                if _sync_bt(data):
                                    log.info('Bluetooth phone started streaming -> auto-pausing QQ Music')
                                    await asyncio.get_running_loop().run_in_executor(None, engine.control, 'pause')
                            elif event_type in ('snapshot', 'state'):
                                if event_type == 'snapshot':
                                    if isinstance(data.get('alarms'), dict):
                                        alarm_ringing = bool(data['alarms'].get('ringing'))
                                        with engine._lock:
                                            engine.alarm_ringing = alarm_ringing
                                            snap_sleep_sess = getattr(engine, 'sleep_session', None) is not None
                                        if alarm_ringing and snap_sleep_sess:
                                            await asyncio.get_running_loop().run_in_executor(None, engine.on_alarm_ringing)
                                    if isinstance(data.get('bluetooth'), dict):
                                        if _sync_bt(data['bluetooth']):
                                            log.info('Bluetooth phone started streaming -> auto-pausing QQ Music')
                                            await asyncio.get_running_loop().run_in_executor(None, engine.control, 'pause')
                                s_obj = data.get('status') if isinstance(data.get('status'), dict) else data
                                if s_obj.get('volume') is not None and not isinstance(s_obj.get('volume'), bool) and not alarm_ringing:
                                    try:
                                        with engine._lock:
                                            if (time.monotonic() - engine._local_vol_mono) >= 1.0:
                                                engine.volume = max(0, min(100, int(float(s_obj['volume']))))
                                    except (TypeError, ValueError, OverflowError):
                                        pass
                                st = s_obj.get('state', 'idle')
                                voice_busy = st != 'idle'
                                engine.set_ducked(voice_busy or alarm_ringing)
                                # If a music command just started and XiaoZhi finishes speaking -> listening,
                                # immediately end the 8s follow-up listening window so music plays right away!
                                if prev_state == 'speaking' and st == 'listening' and engine.just_started and not alarm_ringing:
                                    engine.just_started = False
                                    try:
                                        await sess.post('http://127.0.0.1:8090/api/end', timeout=aiohttp.ClientTimeout(total=2))
                                    except Exception:
                                        pass
                                prev_state = st
                    raise EOFError('XiaoZhi SSE stream closed')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now_m = time.monotonic()
                if now_m - last_err_log >= 60.0:
                    last_err_log = now_m
                    log.warning('XiaoZhi SSE watcher reconnecting (%s)', type(exc).__name__)
                voice_busy = False
                alarm_ringing = False
                prev_bt = None
                with engine._lock:
                    engine.alarm_ringing = False
                    engine.bt_streaming = False
                engine.set_ducked(False)
                prev_state = 'idle'
                await asyncio.sleep(3)


def make_app() -> web.Application:
    app = web.Application(client_max_size=524288)

    def _is_trusted_host(host_hdr) -> bool:
        if not host_hdr or not isinstance(host_hdr, str):
            return False
        raw = host_hdr.strip()
        if not raw or any(c in raw for c in ('/', '\\', '?', '#', '@', ' ', '\t', '\r', '\n')):
            return False
        try:
            sp = urllib.parse.urlsplit(f'//{raw}')
            _ = sp.port
            hn = sp.hostname
        except ValueError:
            return False
        if not hn or sp.username is not None or sp.password is not None or sp.path or sp.query or sp.fragment:
            return False
        hn_ip = hn.split('%', 1)[0] if '%' in hn else hn
        try:
            ip = ipaddress.ip_address(hn_ip)
            if getattr(ip, 'ipv4_mapped', None) is not None:
                ip = ip.ipv4_mapped
            return not (ip.is_global or ip.is_multicast or ip.is_unspecified)
        except ValueError:
            pass
        if '%' in hn:
            return False
        hn_clean = hn[:-1].lower() if hn.endswith('.') else hn.lower()
        if not hn_clean or '..' in hn_clean:
            return False
        if hn_clean == 'localhost' or '.' not in hn_clean:
            return True
        return hn_clean.endswith(('.local', '.lan', '.home', '.internal', '.home.arpa'))

    _KIOSK_LOOPBACK_ORIGINS = frozenset({
        'http://127.0.0.1:8090', 'http://localhost:8090',
        'http://127.0.0.1:8091', 'http://localhost:8091',
        'http://127.0.0.1:8095', 'http://localhost:8095',
    })

    def _allowed_origin(request) -> str:
        origin = request.headers.get('Origin') or ''
        if not origin:
            return ''
        try:
            po = urllib.parse.urlsplit(origin)
            _ = po.port
        except ValueError:
            return ''
        if po.scheme not in ('http', 'https') or not po.hostname or po.path or po.query or po.fragment or po.username is not None:
            return ''
        if request.host and origin == f'{request.scheme}://{request.host}':
            return origin
        if request.remote in ('127.0.0.1', '::1') and origin in _KIOSK_LOOPBACK_ORIGINS:
            return origin
        return ''

    def _apply_cors(resp, allowed):
        if allowed:
            resp.headers['Access-Control-Allow-Origin'] = allowed
            resp.headers['Vary'] = 'Origin'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, If-None-Match'
            resp.headers['Access-Control-Expose-Headers'] = 'ETag'
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        return resp

    @web.middleware
    async def cors_mw(request, handler):
        if not _is_trusted_host(request.headers.get('Host')):
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        allowed = _allowed_origin(request) if origin is not None else ''
        if origin is not None and not allowed:
            raise web.HTTPForbidden()
        sfs = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if sfs == 'cross-site':
            if not (
                request.remote in ('127.0.0.1', '::1')
                and (
                    (origin is not None and origin in _KIOSK_LOOPBACK_ORIGINS)
                    or (origin is None and request.method in ('GET', 'HEAD')
                        and request.headers.get('Sec-Fetch-Dest') == 'iframe'
                        and request.path in ('/', '/music'))
                )
            ):
                raise web.HTTPForbidden()
        elif sfs and sfs not in ('same-origin', 'none') and request.remote not in ('127.0.0.1', '::1') and request.path.startswith('/api/'):
            raise web.HTTPForbidden()
        try:
            if request.method == 'OPTIONS':
                resp = web.Response(status=204)
            else:
                if request.method == 'POST' and request.content_type != 'application/json':
                    raise web.HTTPUnsupportedMediaType()
                resp = await handler(request)
        except web.HTTPException as exc:
            if exc.status != 403:
                _apply_cors(exc, allowed)
            raise
        return _apply_cors(resp, allowed)

    async def _keepalive_304(_req, resp):
        if resp.status == 304:
            resp.headers['Content-Length'] = '0'

    app.middlewares.append(cors_mw)
    app.on_response_prepare.append(_keepalive_304)

    ui_html_cache = {'key': None, 'data': b'', 'etag': ''}
    ui_csp = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.gtimg.cn http://*.gtimg.cn https://gtimg.cn "
        "https://*.qq.com http://*.qq.com https://qq.com https://*.qlogo.cn http://*.qlogo.cn "
        "https://*.qpic.cn http://*.qpic.cn; connect-src 'self'; "
        "frame-ancestors 'self' http://127.0.0.1:8090 http://localhost:8090 "
        "http://127.0.0.1:8095 http://localhost:8095"
    )

    async def index(req):
        try:
            st = os.lstat(UI_HTML_PATH)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 2 * 1024 * 1024:
                raise OSError()
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            if ui_html_cache['key'] != key:
                raw_html = _read_regular_text(UI_HTML_PATH, 2 * 1024 * 1024).encode('utf-8')
                ui_html_cache.update(
                    key=key,
                    data=raw_html,
                    etag=f'"{st.st_ino:x}-{st.st_mtime_ns:x}-{st.st_size:x}"',
                )
        except OSError:
            raise web.HTTPNotFound()
        headers = {
            'Cache-Control': 'no-cache',
            'ETag': ui_html_cache['etag'],
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': ui_csp,
        }
        if req.headers.get('If-None-Match') == ui_html_cache['etag']:
            return web.Response(status=304, headers=headers)
        return web.Response(body=ui_html_cache['data'], content_type='text/html', charset='utf-8', headers=headers)

    async def status(req):
        lite = req.query.get('lite') == '1'
        rq = req.query.get('qseq', '')
        rl = req.query.get('lseq', '')
        qseq = int(rq) if (rq.isascii() and rq.isdigit() and len(rq) <= 18) else None
        lseq = int(rl) if (rl.isascii() and rl.isdigit() and len(rl) <= 18) else None
        return web.json_response(engine.status(lite=lite, qseq=qseq, lseq=lseq))

    async def _json_body(req) -> dict:
        if req.method == 'GET':
            return {}
        try:
            raw = await req.read()
        except Exception:
            raise web.HTTPBadRequest()
        if not raw or not raw.strip():
            return {}
        try:
            b = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest()
        if not isinstance(b, dict):
            raise web.HTTPBadRequest()
        return b

    async def play(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(
            None, engine.play_query, body.get('query', ''), body.get('source', 'auto')
        )
        return web.json_response(res)

    _SAFE_COVER_RE = re.compile(r'^https?://(?:[a-z0-9-]+\.)*(?:gtimg\.cn|qq\.com|qlogo\.cn|qpic\.cn)/', re.I)

    def _safe_cover_url(u: str) -> str:
        if not isinstance(u, str) or not u:
            return ''
        if any(ord(c) <= 0x20 or ord(c) == 0x7f or c == '\\' for c in u):
            return ''
        if _SAFE_COVER_RE.match(u):
            return u
        return ''

    def _sanitize_http_tracks(raw_tracks, selected_index=None):
        if not isinstance(raw_tracks, list):
            return ([], None) if selected_index is not None else []
        roots = []
        for d in LOCAL_DIRS:
            try:
                if d.exists():
                    roots.append(d.resolve())
            except OSError:
                pass
        clean = []
        clean_index = None
        def _s(v, lim=300):
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                return str(v).strip()[:lim]
            return ''

        for raw_index, item in enumerate(raw_tracks[:200]):
            if not isinstance(item, dict):
                continue
            raw_cov = _s(item.get('cover'), 500)
            raw_th = _s(item.get('thumb'), 500) or raw_cov
            tr = {
                'mid': _s(item.get('mid'), 128),
                'media_mid': _s(item.get('media_mid'), 128),
                'title': _s(item.get('title'), 300),
                'artist': _s(item.get('artist'), 300),
                'album': _s(item.get('album'), 300),
                'cover': _safe_cover_url(raw_cov),
                'thumb': _safe_cover_url(raw_th),
                'duration': engine._safe_dur(item.get('duration')),
                'vip': bool(item.get('vip')),
                'source': _s(item.get('source'), 64),
                'quality': _s(item.get('quality'), 64),
                'url': None,
                'is_local': False,
            }
            raw_url = item.get('url')
            valid_local = False
            if item.get('is_local'):
                if isinstance(raw_url, str) and raw_url.strip():
                    try:
                        rp = Path(raw_url).resolve()
                        if (
                            rp.is_file()
                            and rp.suffix.lower() in AUDIO_EXTS
                            and any(rp.is_relative_to(rt) for rt in roots)
                        ):
                            tr['url'] = str(rp)
                            tr['is_local'] = True
                            valid_local = True
                    except (OSError, ValueError):
                        pass
                if not valid_local and not tr['mid']:
                    continue
            if not (tr['mid'] or tr['title'] or valid_local):
                continue
            if raw_index == selected_index:
                clean_index = len(clean)
            clean.append(tr)
        return (clean, clean_index) if selected_index is not None else clean

    async def play_tracks(req):
        body = await _json_body(req)
        open_ui = bool(body.get('open_ui'))
        raw_tracks = body.get('tracks', [])
        try:
            raw_index = body.get('index', 0)
            start_index = 0 if isinstance(raw_index, bool) else int(raw_index)
        except (TypeError, ValueError, OverflowError):
            start_index = 0
        if isinstance(raw_tracks, list) and raw_tracks:
            start_index = max(0, min(start_index, len(raw_tracks) - 1))
            if len(raw_tracks) > 200:
                # Keep the requested song inside the bounded window before sanitizing.
                start = max(0, min(start_index - 100, len(raw_tracks) - 200))
                raw_tracks, start_index = raw_tracks[start:start + 200], start_index - start
        safe_tracks, clean_index = await asyncio.get_running_loop().run_in_executor(
            None, _sanitize_http_tracks, raw_tracks, start_index
        )
        if clean_index is None:
            return web.json_response({'ok': False, 'error': '选中的歌曲已不可播放，请刷新列表'}, status=400)
        res = await asyncio.get_running_loop().run_in_executor(
            None, engine.play_tracks, safe_tracks, clean_index, open_ui
        )
        return web.json_response(res)

    async def control(req):
        body = await _json_body(req)
        action = body.get('action')
        if not isinstance(action, str) or not action.strip():
            return web.json_response({'ok': False, 'error': '缺少控制指令'}, status=400)
        sec_arg = body.get('seconds')
        src_arg = body.get('source')
        if src_arg is not None:
            args = (action.strip(), body.get('index'), sec_arg, src_arg)
        elif sec_arg is not None:
            args = (action.strip(), body.get('index'), sec_arg)
        else:
            args = (action.strip(), body.get('index'))
        res = await asyncio.get_running_loop().run_in_executor(None, engine.control, *args)
        return web.json_response(res)

    async def seek(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(
            None, engine.seek, body.get('sec')
        )
        return web.json_response(res)

    async def volume(req):
        body = await _json_body(req)
        v = await asyncio.get_running_loop().run_in_executor(
            None, engine.set_volume, body.get('volume'), body.get('delta')
        )
        return web.json_response({'ok': True, 'volume': v})

    async def search(req):
        body = await _json_body(req)
        raw_limit = body.get('limit', 25)
        raw_page = body.get('page', 1)
        try:
            limit = 25 if isinstance(raw_limit, bool) else max(1, min(60, int(raw_limit)))
        except (TypeError, ValueError, OverflowError):
            limit = 25
        try:
            page = 1 if isinstance(raw_page, bool) else max(1, min(50, int(raw_page)))
        except (TypeError, ValueError, OverflowError):
            page = 1
        tracks = await asyncio.get_running_loop().run_in_executor(
            None, qq_search, body.get('query', ''), limit, page
        )
        # a full page means there may be more; the touch UI loads the next page on scroll
        return web.json_response({'ok': True, 'tracks': tracks, 'page': page, 'has_more': len(tracks) >= limit})

    async def suggest(req):
        q = _clean_smartbox_query(req.query.get('q', ''))
        cached = _get_cached_smartbox(q)
        if cached is not None:
            return web.json_response(cached)
        res = await asyncio.get_running_loop().run_in_executor(None, qq_smartbox, q)
        return web.json_response(res)

    async def toplist(req):
        rq = req.query.get('id', '')
        top_id = int(rq) if (rq.isascii() and rq.isdigit() and len(rq) <= 9) else 26
        tracks = await asyncio.get_running_loop().run_in_executor(None, qq_toplist, top_id, 40)
        return web.json_response({'ok': True, 'toplists': TOPLISTS, 'tracks': tracks})

    async def playlists(_req):
        loop = asyncio.get_running_loop()
        pls, usb = await asyncio.gather(
            loop.run_in_executor(None, qq_user_playlists),
            loop.run_in_executor(None, search_local, ''),
        )
        return web.json_response({'ok': True, 'playlists': pls, 'usb_tracks': usb})

    async def playlist_tracks(req):
        body = await _json_body(req)
        tracks = await asyncio.get_running_loop().run_in_executor(
            None, qq_playlist_tracks, str(body.get('id', ''))
        )
        return web.json_response({'ok': True, 'tracks': tracks})

    async def fav_api(req):
        # 2026-11-12: 我喜欢 for one song. GET ?mid= -> {liked: true|false|null}; POST {mid, on}
        if req.method == 'GET':
            mid = str(req.query.get('mid') or '')
        else:
            body = await _json_body(req)
            mid = str(body.get('mid') or '')
        if not re.fullmatch(r'[0-9A-Za-z]{6,24}', mid):
            return web.json_response({'ok': False, 'error': '不是 QQ 音乐的歌，没法收藏'}, status=400)
        loop = asyncio.get_running_loop()
        if req.method == 'GET':
            liked = await loop.run_in_executor(None, qq_fav_state, mid)
            return web.json_response({'ok': True, 'mid': mid, 'liked': liked})
        if not isinstance(body.get('on'), bool):
            return web.json_response({'ok': False, 'error': '要收藏还是取消没说清'}, status=400)
        res = await loop.run_in_executor(None, qq_fav_set, mid, body['on'])
        return web.json_response(res)

    async def qr_create(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(None, qq_qr_create, body.get('mode', 'qq'))
        return web.json_response(res)

    async def qr_check(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(
            None, qq_qr_check, body.get('qrsig', ''), body.get('mode', 'qq')
        )
        return web.json_response(res)

    async def bind_uin(req):
        body = await _json_body(req)
        res = await asyncio.get_running_loop().run_in_executor(None, qq_bind_uin, body.get('input', ''))
        return web.json_response(res)

    async def logout(_req):
        res = await asyncio.get_running_loop().run_in_executor(None, qq_logout)
        return web.json_response(res)

    async def set_quality(req):
        body = await _json_body(req)
        def _apply_quality():
            with _CFG_LOCK:
                cfg = load_config()
                prev_q = cfg.get('quality_level', 'lossless')
                if body.get('quality_level') in ('lossless', 'hq', 'standard'):
                    cfg['quality_level'] = body['quality_level']
                if body.get('max_cache_mb') is not None and not isinstance(body.get('max_cache_mb'), bool):
                    try:
                        cfg['max_cache_mb'] = max(10, min(500, int(float(body['max_cache_mb']))))
                    except (TypeError, ValueError, OverflowError):
                        pass
                save_config(cfg)
                return cfg, prev_q
        cfg, prev_q = await asyncio.get_running_loop().run_in_executor(None, _apply_quality)
        new_q = cfg.get('quality_level', 'lossless')
        if new_q != prev_q:
            dq = _default_quality_label(cfg)
            with engine._lock:
                for tr in engine.queue:
                    if isinstance(tr, dict) and not tr.get('is_local'):
                        tr['url'] = None
                        tr['_url_ts'] = None
                        tr['quality'] = dq
                        tr.pop('_retried', None)
                        tr.pop('_drop_retries', None)
                engine.queue_seq += 1
                engine._cached_q_seq = 0
            await asyncio.get_running_loop().run_in_executor(None, engine.save_session)
        return web.json_response({'ok': True, 'quality_level': new_q, 'max_cache_mb': cfg.get('max_cache_mb', 200)})

    async def cache_maintainer():
        """Ensure browser/music cache directories on eMMC never exceed max_cache_mb (default 200MB)."""
        cache_dirs = [
            Path('/home/ubuntu/.cache/thinksmart-qqmusic'),
            Path('/home/ubuntu/.cache/thinksmart-kiosk'),
            Path('/home/ubuntu/.cache/gstreamer-1.0'),
        ]
        def _prune_once():
            cfg = load_config()
            raw_mb = cfg.get('max_cache_mb', 200)
            try:
                mb = 200 if isinstance(raw_mb, bool) else int(raw_mb)
            except (TypeError, ValueError):
                mb = 200
            mb = min(max(mb if mb > 0 else 200, 10), 500)
            max_bytes = mb * 1024 * 1024
            files = []
            total = 0
            for d in cache_dirs:
                if not d.exists() or d.is_symlink():
                    continue
                for root, _, fns in os.walk(str(d)):
                    for fn in fns:
                        fp = os.path.join(root, fn)
                        try:
                            st = os.lstat(fp)
                            if not stat.S_ISREG(st.st_mode):
                                continue
                            total += st.st_size
                            files.append((st.st_mtime, st.st_size, fp))
                        except Exception:
                            pass
            if total > max_bytes:
                files.sort(key=lambda x: x[0])  # oldest first
                for _, sz, fp in files:
                    if total <= max_bytes * 0.8:
                        break
                    try:
                        os.unlink(fp)
                        total -= sz
                    except Exception:
                        pass

        await asyncio.sleep(30)
        while True:
            try:
                await asyncio.get_running_loop().run_in_executor(None, _prune_once)
            except Exception:
                pass
            await asyncio.sleep(600)

    async def voice_wake(req):
        if req.remote not in ('127.0.0.1', '::1'):
            raise web.HTTPForbidden()
        def _do_wake():
            try:
                r = urllib.request.Request('http://127.0.0.1:8090/api/wake', data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
                with _LOCAL_OPENER.open(r, timeout=2.0) as resp:
                    resp.read(64)
                return {'ok': True}
            except Exception as e:
                log.warning('voice_wake failed: %s', type(e).__name__)
                return {'ok': False, 'error': '唤醒小智失败，请稍后重试'}
        res = await asyncio.get_running_loop().run_in_executor(None, _do_wake)
        return web.json_response(res)

    async def sleep_timer(req):
        body = await _json_body(req)
        if req.method == 'GET':
            return web.json_response({'ok': True, 'sleep': engine.sleep_status()})
        res = await asyncio.get_running_loop().run_in_executor(
            None, lambda: engine.set_sleep(body.get('minutes'), body.get('tracks'), body.get('cancel') is True,
                                           body.get('fade'), body.get('until'), body.get('night') is True))
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def sleep_session_api(req):
        # 2026-11-12: sleep sessions. GET: state + choices; POST {action: start|stop|extend|screen_off, ...}
        if req.method == 'GET':
            return web.json_response(engine.sleep_session_info())
        body = await _json_body(req)
        act = body.get('action') or 'start'
        loop = asyncio.get_running_loop()
        if act == 'start':
            snd = body.get('sound')
            if snd is not None and not isinstance(snd, (str, dict)):
                return web.json_response({'ok': False, 'error': '无效声音'}, status=400)
            until = body.get('until')
            until = until if isinstance(until, str) and len(until) <= 8 else None
            res = await loop.run_in_executor(None, lambda: engine.sleep_session_start(
                snd, body.get('minutes'), until, body.get('night') is True, body.get('fade'), body.get('volume')))
        elif act == 'stop':
            res = await loop.run_in_executor(None, engine.sleep_session_stop)
        elif act == 'extend':
            res = await loop.run_in_executor(None, lambda: engine.sleep_session_extend(body.get('minutes', 30)))
        elif act == 'screen_off':
            res = await loop.run_in_executor(None, engine.sleep_screen_off)
        elif act == 'preview':
            snd = body.get('sound')
            if snd is not None and not isinstance(snd, (str, dict)):
                return web.json_response({'ok': False, 'error': '无效声音'}, status=400)
            res = await loop.run_in_executor(None, lambda: engine.sleep_preview(snd, body.get('volume'), body.get('seconds', 30)))
        elif act == 'preview_stop':
            res = await loop.run_in_executor(None, engine.sleep_preview_stop)
        elif act == 'night_view':
            res = await loop.run_in_executor(None, engine.sleep_night_view, body.get('on') is True)
        elif act == 'wake':
            res = await loop.run_in_executor(None, engine.sleep_set_wake, body.get('on'), body.get('time'))
        else:
            return web.json_response({'ok': False, 'error': '未知操作'}, status=400)
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def lyrics_api(req):
        with engine._lock:
            seq = getattr(engine, 'lyrics_seq', 1)
            t = engine.queue[engine.index] if (engine.playing and 0 <= engine.index < len(engine.queue)) else None
            mid = str(t.get('mid') or '') if t else ''
            lrc = list(engine.lyrics[:300]) if engine.playing else []
        etag = f'"{seq}-{mid}-{len(lrc)}"'
        if req.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag, 'Cache-Control': 'no-cache'})
        return web.json_response({'ok': True, 'seq': seq, 'mid': mid, 'lyrics': lrc},
                                 headers={'ETag': etag, 'Cache-Control': 'no-cache'})

    async def ambient_api(req):
        if req.method == 'GET':
            with engine._lock:
                cur = engine._ambient_public_locked() if hasattr(engine, '_ambient_public_locked') else None
                vol = getattr(engine, 'volume', 55)
            return web.json_response({
                'ok': True,
                'ambient': cur,
                'volume': vol,
                'presets': list(AMBIENT_PRESETS.values()),
                'recipes': list(AMBIENT_RECIPES.values()),
            })
        body = await _json_body(req)
        preset = body.get('preset', 'off')
        if not isinstance(preset, str):
            return web.json_response({'ok': False, 'error': '无效环境音'}, status=400)
        res = await asyncio.get_running_loop().run_in_executor(
            None,
            engine.set_ambient,
            preset,
            body.get('volume'),
            body.get('layer'),
            body.get('layer_gain'),
        )
        return web.json_response(res, status=200 if res.get('ok') else 400)

    async def sleep_watcher():
        loop = asyncio.get_running_loop()
        n = 0
        while True:
            await asyncio.sleep(0.5)
            n += 1
            try:
                if engine.sleep_mode == 'time':
                    await loop.run_in_executor(None, engine.sleep_tick)
                if n % 4 == 0:
                    await loop.run_in_executor(None, engine.sleep_housekeeping)
                    await loop.run_in_executor(None, engine.sleep_wake_tick)
            except Exception:
                log.exception('sleep timer tick failed')

    bg_tasks = []

    async def on_startup(_app):
        bg_tasks.append(asyncio.create_task(watch_xiaozhi_events()))
        bg_tasks.append(asyncio.create_task(cache_maintainer()))
        bg_tasks.append(asyncio.create_task(sleep_watcher()))

    async def on_cleanup(_app):
        for t in bg_tasks:
            t.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        bg_tasks.clear()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get('/', index)
    app.router.add_get('/music', index)
    app.router.add_get('/api/music/status', status)
    app.router.add_post('/api/music/play', play)
    app.router.add_post('/api/music/play_tracks', play_tracks)
    app.router.add_post('/api/music/control', control)
    app.router.add_post('/api/music/seek', seek)
    app.router.add_post('/api/music/volume', volume)
    app.router.add_post('/api/music/search', search)
    app.router.add_get('/api/music/suggest', suggest)
    app.router.add_post('/api/music/voice_wake', voice_wake)
    app.router.add_get('/api/music/toplist', toplist)
    app.router.add_get('/api/music/playlists', playlists)
    app.router.add_post('/api/music/playlist/tracks', playlist_tracks)
    app.router.add_get('/api/music/fav', fav_api)
    app.router.add_post('/api/music/fav', fav_api)
    app.router.add_post('/api/music/qr/create', qr_create)
    app.router.add_post('/api/music/qr/check', qr_check)
    app.router.add_post('/api/music/bind', bind_uin)
    app.router.add_post('/api/music/logout', logout)
    app.router.add_post('/api/music/quality', set_quality)
    app.router.add_get('/api/music/sleep', sleep_timer)
    app.router.add_post('/api/music/sleep', sleep_timer)
    app.router.add_get('/api/music/lyrics', lyrics_api)
    app.router.add_get('/api/music/ambient', ambient_api)
    app.router.add_get('/api/music/sleep_session', sleep_session_api)
    app.router.add_post('/api/music/sleep_session', sleep_session_api)
    app.router.add_post('/api/music/ambient', ambient_api)
    return app


async def main():
    os.umask(0o077)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix='qqm-io')
    )
    aec_srv = start_aec_unix_server()
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8091)
    await site.start()
    log.info('Standalone QQ Music service listening on http://0.0.0.0:8091/')
    stop_ev = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_ev.set)
        except NotImplementedError:
            pass
    try:
        await stop_ev.wait()
    finally:
        with contextlib.suppress(Exception):
            await site.stop()
        if aec_srv is not None:
            with contextlib.suppress(Exception):
                aec_srv.close()
            with contextlib.suppress(OSError):
                if os.path.lexists(AEC_SOCK_PATH) and stat.S_ISSOCK(os.lstat(AEC_SOCK_PATH).st_mode):
                    os.unlink(AEC_SOCK_PATH)
        with _SESSION_LOCK:
            engine._save_session_locked()
            with engine._lock:
                engine._shutting_down = True
                engine.playing = False
                procs = [p for p in (engine._proc, getattr(engine, '_proc2', None), engine._pacat) if p is not None]
                engine._stop_proc_locked()
        for p in procs:
            try:
                p.wait(timeout=1.0)
            except Exception:
                pass
        with engine._aec_lock:
            for s in list(engine.aec_clients):
                try:
                    s.close()
                except Exception:
                    pass
            engine.aec_clients.clear()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
