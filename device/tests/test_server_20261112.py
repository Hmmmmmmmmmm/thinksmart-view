#!/opt/thinksmart-addons/venv/bin/python
"""Regression suite for 2026-11-12 ThinkSmart Server Monitor Round 1–4 upgrades (Antigravity)."""
import asyncio
import copy
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


REPO_DEVICE = Path(__file__).resolve().parent.parent


def _asset(installed: str, repo_rel: str) -> Path:
    p = Path(installed)
    return p if p.exists() else (REPO_DEVICE / repo_rel)


def _load_monitor_module():
    mon_dir = str(_asset('/usr/local/bin/thinksmart_display.py', 'monitor/thinksmart_display.py').parent)
    if mon_dir not in sys.path:
        sys.path.insert(0, mon_dir)
    path = str(_asset('/usr/local/bin/thinksmart-monitor.py', 'monitor/thinksmart-monitor.py'))
    loader = importlib.machinery.SourceFileLoader('ts_monitor_20261112', path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_monitor_module()

SAMPLE_WS = {
    'now': 1794471273000,
    'online': 1,
    'servers': [
        {
            'id': 3,
            'name': '主力节点',
            'country_code': 'us',
            'last_active': '2026-11-12T16:14:31.91220203+08:00',
            'public_note': json.dumps({
                'billingDataMod': {
                    'endDate': '2027-06-01T00:00:00.000Z',
                    'autoRenewal': '1',
                    'cycle': 'Year',
                    'amount': '$20.00',
                },
                'planDataMod': {
                    'bandwidth': '500Mbps',
                    'trafficVol': '1TB',
                    'trafficType': '1',
                    'IPv4': '1',
                    'IPv6': '0',
                    'networkRoute': '三网优化',
                },
            }),
            'host': {
                'platform': 'debian',
                'cpu': ['AMD EPYC-Rome Processor 1 Virtual Core'],
                'mem_total': 2059100160,
                'disk_total': 20922114048,
                'swap_total': 3221221376,
                'arch': 'x86_64',
            },
            'state': {
                'cpu': 2.5,
                'mem_used': 309628928,
                'disk_used': 9990787072,
                'net_in_transfer': 13196630352,
                'net_out_transfer': 12712014458,
                'net_in_speed': 186644,
                'net_out_speed': 178614,
                'uptime': 94391,
                'load_1': 0.14,
                'load_5': 0.16,
                'load_15': 0.10,
                'tcp_conn_count': 409,
                'udp_conn_count': 11,
                'process_count': 90,
            },
        },
        {
            'id': 10,
            'name': '备用节点',
            'country_code': 'vn',
            'last_active': '2026-11-12T16:14:32.769284534+08:00',
            'public_note': json.dumps({
                'billingDataMod': {'endDate': '2029-06-01T00:00:00.000Z', 'cycle': 'Year', 'amount': '$30'},
                'planDataMod': {
                    'bandwidth': '1Gbps',
                    'trafficVol': '2TB',
                    'trafficType': '2',
                    'IPv6': '1',
                    'networkRoute': '普通网络',
                },
            }),
            'host': {
                'platform': 'debian',
                'cpu': ['AMD EPYC-Milan Processor 2 Virtual Core'],
                'mem_total': 3073130496,
                'disk_total': 73834295296,
                'swap_total': 7441731584,
                'arch': 'x86_64',
                'virtualization': 'kvm',
            },
            'state': {
                'cpu': 6.8,
                'mem_used': 2150690816,
                'swap_used': 1739579392,
                'disk_used': 27639738368,
                'net_in_transfer': 601010700,
                'net_out_transfer': 114565800,
                'net_in_speed': 9414,
                'net_out_speed': 1990,
                'uptime': 93537,
                'load_1': 0.34,
                'load_5': 0.21,
                'load_15': 0.13,
                'tcp_conn_count': 53,
                'udp_conn_count': 10,
                'process_count': 302,
            },
        },
    ],
}

SAMPLE_SVC = {
    'success': True,
    'data': {
        'services': {
            '4': {
                'service_name': '移动',
                'current_up': 240,
                'current_down': 0,
                'total_up': 368520,
                'total_down': 272,
                'delay': [250.0, 260.0, 238.7],
                'up': [11520, 11520, 15502],
                'down': [0, 0, 26],
            },
            '2': {
                'service_name': '联通',
                'current_up': 235,
                'current_down': 5,
                'total_up': 368732,
                'total_down': 59,
                'delay': [210.0, 315.1, 281.7],
                'up': [11520, 20935, 15516],
                'down': [0, 9, 12],
            },
            '3': {
                'service_name': '电信',
                'current_up': 240,
                'current_down': 0,
                'total_up': 368700,
                'total_down': 90,
                'delay': [265.0, 290.0, 283.6],
                'up': [11520, 11520, 15519],
                'down': [0, 1, 9],
            },
        },
        'cycle_transfer_stats': {
            '4': {
                'name': '主力节点双向月流量',
                'from': '2026-10-16T00:00:00+08:00',
                'to': '2026-11-16T00:00:00+08:00',
                'max': 1000000000000,
                'transfer': {'3': 322197187070},
            },
        },
    },
}

SAMPLE_SERVER_SVC_3 = {
    'success': True,
    'data': [
        {
            'monitor_id': 4,
            'server_id': 3,
            'monitor_name': '移动',
            'server_name': '主力节点',
            'created_at': [1794400000000 + i * 60000 for i in range(400)],
            'avg_delay': [156.0 if i != 120 else 1214.0 for i in range(399)] + [154.2],
        },
        {
            'monitor_id': 3,
            'server_id': 3,
            'monitor_name': '电信',
            'server_name': '主力节点',
            'created_at': [1794400000000 + i * 60000 for i in range(400)],
            'avg_delay': [157.5 for _ in range(399)] + [157.6],
        },
        {
            'monitor_id': 2,
            'server_id': 3,
            'monitor_name': '联通',
            'server_name': '主力节点',
            'created_at': [1794400000000 + i * 60000 for i in range(400)],
            'avg_delay': [165.0 if i != 50 else 3809.7 for i in range(399)] + [162.2],
        },
    ],
}

TRAFFIC_CFG = {
    'rule_id': 4,
    'server_id': 3,
    'limit_bytes': 1000000000000,
    'complete_from': '2026-11-16T00:00:00+08:00',
    'calibration': {
        'from': '2026-10-16T00:00:00+08:00',
        'to': '2026-11-16T00:00:00+08:00',
        'observed_bytes': 2725097925,
        'used_bytes': 716570000000,
        'extra_bytes': 300000000000,
    },
}


class ServerMonitorUpgradeTests(unittest.TestCase):
    def test_cpu_spec_parser(self):
        self.assertEqual(MOD._parse_cpu_spec(['AMD EPYC-Milan Processor 4 Virtual Core']), ('AMD EPYC-Milan', 4))
        self.assertEqual(
            MOD._parse_cpu_spec(['Intel(R) Xeon(R) Gold 6252 CPU @ 2.10GHz 1 Virtual Core']),
            ('Intel Xeon Gold 6252', 1),
        )
        self.assertEqual(MOD._parse_cpu_spec([]), (None, 0))
        self.assertEqual(MOD._parse_cpu_spec('bad'), (None, 0))

    def test_normalize_extended_telemetry_without_raw_host(self):
        out = MOD.normalize(SAMPLE_WS, [3, 10])
        self.assertEqual(len(out), 2)
        s3, s10 = out
        self.assertNotIn('host', s3)
        self.assertNotIn('host', s10)
        self.assertTrue(s3['alive'])
        self.assertEqual(s3['cpu_model'], 'AMD EPYC-Rome')
        self.assertEqual(s3['cpu_cores'], 1)
        self.assertEqual(s3['route'], '三网优化')
        self.assertEqual(s3['auto_renewal'], True)
        self.assertEqual(s3['ipv6'], False)
        self.assertEqual(s3['tcp_conn_count'], 409)
        self.assertEqual(s10['virt'], 'kvm')
        self.assertEqual(s10['swap_total'], 7441731584)
        self.assertEqual(s10['swap_used'], 1739579392)
        self.assertAlmostEqual(s10['load_5'], 0.21)
        self.assertAlmostEqual(s10['load_15'], 0.13)
        self.assertEqual(s10['ipv6'], True)
        self.assertEqual(s10['traffic_type'], '双向')

    def test_carriers_strict_unicom_telecom_mobile_order(self):
        cs = MOD.normalize_carriers(SAMPLE_SVC)
        self.assertEqual([c['name'] for c in cs], ['联通', '电信', '移动'])
        self.assertEqual(cs[0]['status'], 'warn')  # 联通 5 / 240 = 2.1% loss
        self.assertAlmostEqual(cs[0]['loss_pct'], 2.1)
        self.assertEqual(cs[1]['status'], 'ok')
        self.assertEqual(cs[2]['status'], 'ok')
        self.assertEqual(MOD.normalize_carriers({'success': False}), [])

    def test_per_server_service_parser_windows_and_p96_spike_protection(self):
        cs = MOD.normalize_server_service(SAMPLE_SERVER_SVC_3)
        self.assertEqual([c['name'] for c in cs], ['联通', '电信', '移动'])
        uni = cs[0]
        self.assertAlmostEqual(uni['latest_ms'], 162.2)
        self.assertEqual(uni['status'], 'ok')
        self.assertEqual(len(uni['win']['1h']['pts']), 60)
        self.assertEqual(len(uni['win']['6h']['pts']), 72)
        self.assertLess(uni['win']['24h']['p96'], 200.0)
        self.assertGreater(uni['win']['24h']['max'], 3000.0)

    def test_traffic_extra_bytes_and_midnight_rollover_grace(self):
        now = dt.datetime.fromisoformat('2026-11-12T16:15:00+08:00')
        cur = MOD.normalize_traffic(SAMPLE_SVC, TRAFFIC_CFG, now=now)
        self.assertTrue(cur['calibrated'])
        self.assertEqual(cur['limit_bytes'], 1300000000000)
        self.assertEqual(cur['extra_bytes'], 300000000000)
        self.assertEqual(cur['rule_name'], '主力节点双向月流量')

        roll_svc = copy.deepcopy(SAMPLE_SVC)
        roll_svc['data']['cycle_transfer_stats']['4'] = {
            'name': '主力节点双向月流量',
            'from': '2026-11-16T00:00:00+08:00',
            'to': '2026-12-16T00:00:00+08:00',
            'max': 1000000000000,
            'transfer': {},
        }
        roll_now = dt.datetime.fromisoformat('2026-11-16T00:05:00+08:00')
        fresh = MOD.normalize_traffic(roll_svc, TRAFFIC_CFG, now=roll_now)
        self.assertFalse(fresh['calibrated'])
        self.assertTrue(fresh['complete'])
        self.assertEqual(fresh['used_bytes'], 0)
        self.assertEqual(fresh['limit_bytes'], 1000000000000)
        self.assertEqual(fresh['remaining_bytes'], 1000000000000)
        self.assertNotIn('extra_bytes', fresh)

        late_now = dt.datetime.fromisoformat('2026-11-16T00:25:00+08:00')
        with self.assertRaises(ValueError):
            MOD.normalize_traffic(roll_svc, TRAFFIC_CFG, now=late_now)

        other_svc = copy.deepcopy(roll_svc)
        other_svc['data']['cycle_transfer_stats']['4']['transfer'] = {'12': 100}
        with self.assertRaises(ValueError):
            MOD.normalize_traffic(other_svc, TRAFFIC_CFG, now=roll_now)

    def test_hot_reload_and_speed_history_gap_clear(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(
                json.dumps([
                    {'id': 3, 'name': '主力节点', 'country': 'us'},
                    {'id': 10, 'name': '备用节点', 'country': 'vn'},
                ]),
                encoding='utf-8',
            )
            mon = MOD.Monitor(p)
            mon.connected = True
            mon.update(SAMPLE_WS)
            mon.update(SAMPLE_WS)
            snap1 = mon.snapshot()
            self.assertEqual(len(snap1['speed_hist']['3']), 2)
            # Simulate >12s gap across disconnect -> old speed history cleared before new sample
            mon._last_speed_mono -= 20.0
            mon.update(SAMPLE_WS)
            self.assertEqual(len(mon.snapshot()['speed_hist']['3']), 1)

            (p / 'servers.json').write_text(
                json.dumps([
                    {'id': 10, 'name': '备用节点Pro', 'country': 'vn'},
                    {'id': 3, 'name': '主力节点Pro', 'country': 'us'},
                ]),
                encoding='utf-8',
            )
            self.assertTrue(mon._reload_servers_config(force=True))
            self.assertEqual(mon.ids, [10, 3])
            self.assertEqual([s['name'] for s in mon.snapshot()['servers']], ['备用节点Pro', '主力节点Pro'])

    def test_short_voice_lease_never_shrinks_ui_lease(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(json.dumps([{'id': 3, 'name': '主力节点', 'country': 'us'}]), encoding='utf-8')
            mon = MOD.Monitor(p)
            mon.touch(lease=15.0)
            self.assertTrue(mon.viewed_recently())
            rem_short = MOD.VIEW_IDLE - (time.monotonic() - mon.last_view)
            self.assertLessEqual(rem_short, 16.0)
            self.assertGreater(rem_short, 12.0)
            # Full UI touch extends to ~90s, and a subsequent short voice touch must NOT shrink it
            mon.touch()
            ui_anchor = mon.last_view
            mon.touch(lease=15.0)
            self.assertGreaterEqual(mon.last_view, ui_anchor)

    def test_voice_summary_and_billing_modes(self):
        now = dt.datetime.fromisoformat('2026-11-12T16:15:00+08:00')
        snap = {
            'connected': True,
            'age': 0.5,
            'servers': MOD.normalize(SAMPLE_WS, [3, 10]),
            'traffic': MOD.normalize_traffic(SAMPLE_SVC, TRAFFIC_CFG, now=now),
            'carriers': MOD.normalize_server_service(SAMPLE_SERVER_SVC_3),
            'unicom_map': {'3': {'ms': 162.2, 'status': 'ok'}, '10': {'ms': 239.9, 'status': 'bad'}},
        }
        ov = MOD.build_voice_summary(snap, focus='overview')
        self.assertIn('2/2 台服务器全部在线', ov)
        self.assertIn('剩余流量约 264 GB', ov)
        self.assertIn('主节点延迟 联通 162、电信 158、移动 154 毫秒', ov)

        ca = MOD.build_voice_summary(snap, focus='carriers')
        self.assertEqual(ca, '主力节点当前三网延迟：联通 162 毫秒，电信 158 毫秒，移动 154 毫秒。')

        bl = MOD.build_voice_summary(snap, focus='billing')
        self.assertIn('50 美元', bl)
        self.assertIn('折合人民币约每年', bl)

        vn = MOD.build_voice_summary(snap, query='备用节点')
        self.assertIn('备用节点当前在线', vn)
        self.assertIn('Swap 已用', vn)
        self.assertIn('联通延迟 240 毫秒', vn)

    def test_compute_badge_priorities(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(
                json.dumps([
                    {'id': 3, 'name': '主力节点', 'country': 'us', 'expires': '2027-06-01T00:00:00.000Z'},
                    {'id': 10, 'name': '备用节点', 'country': 'vn', 'expires': '2029-06-01T00:00:00.000Z'},
                ]),
                encoding='utf-8',
            )
            mon = MOD.Monitor(p)
            mon.update(SAMPLE_WS)
            self.assertEqual(mon.compute_badge()['text'], '')
            mon.traffic = {
                'available': True, 'server_id': 3,
                'used_bytes': 1150000000000, 'limit_bytes': 1300000000000,
                'to': '2099-11-16T00:00:00+08:00',
            }
            b_q = mon.compute_badge()
            self.assertIn('流量', b_q['text'])
            self.assertTrue(b_q['amber'])
            mon.servers[1]['alive'] = False
            b_off = mon.compute_badge()
            self.assertEqual(b_off['text'], '1离线')
            self.assertTrue(b_off['bad'])

    def test_static_ui_and_bridge_contracts(self):
        idx = _asset('/usr/local/share/thinksmart-monitor/index.html', 'monitor/index.html').read_text(encoding='utf-8')
        for token in ('--bg:#cfc5b7', '--panel:#e3dcd0', 'POLL_MS=1500', 'id="srvShade"', 'id="ispShade"',
                      'id="fleetShade"', 'id="dmUniSvg"', 'id="pinBtn"', '主力节点 · 三网实时延迟监控',
                      '联通回程延时波形', 'fastRetry', 'fastRetryT', 'lastNeedFast',
                      'id="dmIspSwitch"', 'id="dmUniWrap"', 'id="flEvents"', 'bindChartScrub', 'applySubCmd', 'resolveTraffic',
                      'monoNow', 'id="flLinks"', 'fleetSort', 'fleetEvExpanded', 'parseTrafficLimitBytes', 'renderExtraQuotas'):
            self.assertIn(token, idx)
        self.assertNotIn('#5256ba', idx)
        self.assertNotIn('infinite', idx)

        ov = _asset('/usr/local/share/thinksmart-monitor/home_overlay.js', 'monitor/home_overlay.js').read_text(encoding='utf-8')
        for token in ('pollServerBadge', '/api/badge', 'stopOverlayClip', '#e3dbce', 'lastSrvOpenSeq', 'scheduleVoiceSrvCheck'):
            self.assertIn(token, ov)

        home_idx = _asset('/usr/local/share/thinksmart-home/index.html', 'home/index.html').read_text(encoding='utf-8')
        self.assertIn('applySrvBadge', home_idx)

        dsp = _asset('/usr/local/share/thinksmart-monitor/display.html', 'monitor/display.html').read_text(encoding='utf-8')
        for token in ('--bg:#f6f2ea', '--ctl:#e8e1d5', 'commitDispatched', 'height:100%;overflow:hidden'):
            self.assertIn(token, dsp)

        bridge_path = str(_asset('/usr/local/lib/thinksmart-aec-bridge/sitecustomize.py', 'qqmusic/aec_bridge_sitecustomize.py'))
        spec = importlib.util.spec_from_file_location('ts_bridge_srv_check', bridge_path)
        bmod = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'argv', ['python', '-c', 'noop']), patch.object(sys, 'orig_argv', ['python', '-c', 'noop'], create=True):
            spec.loader.exec_module(bmod)
        srv_tool = next(t for t in bmod.MUSIC_TOOLS if t['name'] == 'self.server.status')
        props = srv_tool['inputSchema']['properties']
        self.assertIn('billing', props['focus']['enum'])
        self.assertIn('events', props['focus']['enum'])
        self.assertIn('compare', props['focus']['enum'])
        self.assertIn('open_ui', props)

    def test_anti_jitter_3probe_median_prevents_false_bad(self):
        now_ms = int(time.time() * 1000)
        raw = {
            'success': True,
            'data': [
                {
                    'monitor_id': 1, 'monitor_name': '联通',
                    'created_at': [now_ms - 120000, now_ms - 60000, now_ms],
                    'avg_delay': [164.0, 168.0, 910.0],
                },
                {
                    'monitor_id': 2, 'monitor_name': '电信',
                    'created_at': [now_ms - 120000, now_ms - 60000, now_ms],
                    'avg_delay': [1520.0, 1610.0, 1680.0],
                },
                {
                    'monitor_id': 3, 'monitor_name': '移动',
                    'created_at': [now_ms - 120000, now_ms - 60000, now_ms],
                    'avg_delay': [82.0, 0.0, 0.0],
                },
            ],
        }
        cs = MOD.normalize_server_service(raw)
        self.assertEqual([c['name'] for c in cs], ['联通', '电信', '移动'])
        self.assertEqual(cs[0]['status'], 'warn')  # single-minute spike -> warn, not bad
        self.assertEqual(cs[1]['status'], 'bad')   # sustained high latency -> bad
        self.assertEqual(cs[2]['status'], 'bad')   # two consecutive drops -> bad

    def test_offline_and_recovery_events_persistence_and_voice(self):
        now_s = time.time()
        now_ms = int(now_s * 1000)
        iso_live = dt.datetime.fromtimestamp(now_s - 2, tz=dt.timezone.utc).isoformat()
        iso_dead = dt.datetime.fromtimestamp(now_s - 720, tz=dt.timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(
                json.dumps([
                    {'id': 3, 'name': '主力节点', 'country': 'us'},
                    {'id': 12, 'name': '节点B', 'country': 'jp'},
                ]),
                encoding='utf-8',
            )
            mon = MOD.Monitor(p, state_dir=p)
            mon.connected = True
            mon.update({'now': now_ms, 'servers': [{'id': 3, 'last_active': iso_live}, {'id': 12, 'last_active': iso_dead}]})
            s_off = mon.snapshot()
            self.assertIn('12', s_off['offline_since'])
            self.assertIn('离线', MOD.build_voice_summary(s_off, query='节点B'))

            mon.update({'now': now_ms + 5000, 'servers': [{'id': 3, 'last_active': iso_live}, {'id': 12, 'last_active': iso_live}]})
            s_rec = mon.snapshot()
            self.assertNotIn('12', s_rec['offline_since'])
            self.assertEqual(s_rec['events'][0]['type'], 'recovered')
            self.assertGreaterEqual(s_rec['events'][0]['duration_s'], 700)

            mon2 = MOD.Monitor(p, state_dir=p)
            self.assertEqual(mon2.snapshot()['events'][0]['type'], 'recovered')
            ev_txt = MOD.build_voice_summary(s_rec, query='最近掉过线吗', focus='events')
            self.assertIn('节点B恢复在线', ev_txt)

    def test_traffic_cached_anti_flicker_and_unicom_map_expiry(self):
        now = dt.datetime.fromisoformat('2026-11-12T16:15:00+08:00')
        valid_tr = dict(MOD.normalize_traffic(SAMPLE_SVC, TRAFFIC_CFG, now=now), to='2099-11-16T00:00:00+08:00')
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(json.dumps([{'id': 3, 'name': '主力节点'}]), encoding='utf-8')
            mon = MOD.Monitor(p)
            mon.connected = True
            mon.update(SAMPLE_WS)
            mon.traffic = {'available': False, 'server_id': 3}
            mon._last_valid_traffic = valid_tr
            mon._last_valid_traffic_mono = time.monotonic()
            mon._srv_latency_cache[3] = {'at': time.monotonic() - 1000.0, 'unicom': {'latest_ms': 162.0, 'status': 'ok'}}
            snap = mon.snapshot()
            self.assertFalse(snap['traffic']['available'])
            self.assertTrue(snap['traffic_cached']['available'])
            self.assertEqual(snap['unicom_map'], {})
            spoken = MOD.build_voice_summary(snap, focus='traffic')
            self.assertIn('1300 GB', spoken)

    def test_round4_minute_gap_zerofill_and_offline_staleness(self):
        base_ms = 1794400000000
        # 10-minute gap between minute 1 and minute 11 -> 9 missing minutes must be zero-filled
        raw_gap = {
            'success': True,
            'data': [
                {
                    'monitor_id': 2,
                    'monitor_name': '联通',
                    'created_at': [base_ms, base_ms + 60000, base_ms + 660000, base_ms + 720000],
                    'avg_delay': [160.0, 162.0, 161.0, 163.0],
                }
            ],
        }
        cs_gap = MOD.normalize_server_service(raw_gap, now_ts=(base_ms + 720000) / 1000.0)
        self.assertEqual(len(cs_gap), 1)
        u_gap = cs_gap[0]
        self.assertFalse(u_gap['stale'])
        self.assertAlmostEqual(u_gap['latest_ms'], 163.0)
        # 4 valid + 9 zero-filled = 13 total buckets -> 9/13 = 69.2% loss in 1h window
        self.assertEqual(len(u_gap['win']['1h']['pts']), 13)
        self.assertGreater(u_gap['win']['1h']['loss'], 65.0)
        drop_flags = [p[1] for p in u_gap['win']['1h']['pts']]
        self.assertEqual(drop_flags.count(2), 9)

        # Trailing staleness (>4 min behind now_ts or server_alive=False) -> stale=True, latest_ms=None, status='bad'
        cs_stale = MOD.normalize_server_service(raw_gap, now_ts=(base_ms + 720000) / 1000.0 + 1800.0)
        self.assertTrue(cs_stale[0]['stale'])
        self.assertIsNone(cs_stale[0]['latest_ms'])
        self.assertEqual(cs_stale[0]['status'], 'bad')
        self.assertGreaterEqual(cs_stale[0]['end_ts'], (base_ms + 720000) / 1000.0 + 1680.0)

        cs_dead = MOD.normalize_server_service(raw_gap, now_ts=(base_ms + 720000) / 1000.0, server_alive=False)
        self.assertTrue(cs_dead[0]['stale'])
        self.assertIsNone(cs_dead[0]['latest_ms'])
        self.assertEqual(cs_dead[0]['status'], 'bad')

    def test_round4_idle_probe_adaptive_intervals_and_50_events_cap(self):
        day_ts = dt.datetime.fromisoformat('2026-11-12T14:30:00+08:00').timestamp()
        night_ts = dt.datetime.fromisoformat('2026-11-12T03:15:00+08:00').timestamp()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(
                json.dumps([
                    {'id': 3, 'name': '主力节点', 'country': 'us'},
                    {'id': 10, 'name': '备用节点', 'country': 'vn'},
                ]),
                encoding='utf-8',
            )
            mon = MOD.Monitor(p, state_dir=p)
            mon.connected = True
            mon.update(SAMPLE_WS)
            self.assertEqual(mon._next_idle_probe_interval(now_wall=day_ts), 600.0)
            self.assertEqual(mon._next_idle_probe_interval(now_wall=night_ts), 900.0)

            # Once any node is offline, idle probe accelerates to 60s even during night hours
            mon.servers[1]['alive'] = False
            self.assertEqual(mon._next_idle_probe_interval(now_wall=day_ts), 60.0)
            self.assertEqual(mon._next_idle_probe_interval(now_wall=night_ts), 60.0)

            # Verify event history cap is 50 (expanded from 16)
            self.assertEqual(MOD.MAX_EVENTS_HISTORY, 50)
            now_s = time.time()
            iso_live = dt.datetime.fromtimestamp(now_s - 2, tz=dt.timezone.utc).isoformat()
            iso_dead = dt.datetime.fromtimestamp(now_s - 600, tz=dt.timezone.utc).isoformat()
            for i in range(30):
                mon.update({'now': int(now_s * 1000), 'servers': [{'id': 3, 'last_active': iso_live}, {'id': 10, 'last_active': iso_dead}]})
                mon.update({'now': int(now_s * 1000), 'servers': [{'id': 3, 'last_active': iso_live}, {'id': 10, 'last_active': iso_live}]})
            self.assertEqual(len(mon.snapshot()['events']), 50)

    def test_round4_badge_disk_mem_carrier_and_extra_traffic_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(
                json.dumps([
                    {'id': 3, 'name': '主力节点', 'country': 'us', 'expires': '2028-06-01T00:00:00.000Z'},
                    {'id': 10, 'name': '备用节点', 'country': 'vn', 'expires': '2029-06-01T00:00:00.000Z'},
                ]),
                encoding='utf-8',
            )
            mon = MOD.Monitor(p)
            mon.update(SAMPLE_WS)
            self.assertEqual(mon.compute_badge()['text'], '')

            # Carrier degradation -> amber '线路波动'; all 3 carriers bad -> red '三网故障'
            mon._carriers_updated_mono = time.monotonic()
            mon.carriers = [
                {'name': '联通', 'status': 'bad', 'latest_ms': 950.0},
                {'name': '电信', 'status': 'ok', 'latest_ms': 160.0},
                {'name': '移动', 'status': 'ok', 'latest_ms': 155.0},
            ]
            b_wave = mon.compute_badge()
            self.assertEqual(b_wave['text'], '线路波动')
            self.assertTrue(b_wave['amber'])
            self.assertFalse(b_wave['bad'])

            mon.carriers = [
                {'name': '联通', 'status': 'bad', 'latest_ms': None},
                {'name': '电信', 'status': 'bad', 'latest_ms': None},
                {'name': '移动', 'status': 'bad', 'latest_ms': None},
            ]
            b_allbad = mon.compute_badge()
            self.assertEqual(b_allbad['text'], '三网故障')
            self.assertTrue(b_allbad['bad'])
            mon.carriers = []

            # Memory >= 92% -> red '内存94%'
            mon.servers[1]['mem_total'] = 1000
            mon.servers[1]['mem_used'] = 940
            b_mem = mon.compute_badge()
            self.assertEqual(b_mem['text'], '内存94%')
            self.assertTrue(b_mem['bad'])

            # Disk >= 92% takes priority over memory -> red '磁盘96%'
            mon.servers[1]['disk_total'] = 1000
            mon.servers[1]['disk_used'] = 960
            b_dsk = mon.compute_badge()
            self.assertEqual(b_dsk['text'], '磁盘96%')
            self.assertTrue(b_dsk['bad'])

            # Extra traffic rule >= 95% -> red '流量97%'
            mon.extra_traffic = {
                '10': {
                    'rule_id': 9, 'server_id': 10, 'available': True, 'complete': True,
                    'used_bytes': 970_000_000_000, 'limit_bytes': 1_000_000_000_000,
                    'remaining_bytes': 30_000_000_000, 'to': '2099-12-01T00:00:00+08:00',
                }
            }
            b_extr = mon.compute_badge()
            self.assertEqual(b_extr['text'], '流量97%')
            self.assertTrue(b_extr['bad'])

    def test_round4_extra_traffic_rules_and_zero_fork_local_links(self):
        now = dt.datetime.fromisoformat('2026-11-12T16:15:00+08:00')
        svc_multi = copy.deepcopy(SAMPLE_SVC)
        svc_multi['data']['cycle_transfer_stats']['8'] = {
            'name': '备用节点双向月流量',
            'from': '2026-11-01T00:00:00+08:00',
            'to': '2026-12-01T00:00:00+08:00',
            'max': 2_000_000_000_000,
            'transfer': {'10': 500_000_000_000},
        }
        ex = MOD.normalize_extra_traffic_rules(svc_multi, skip_rule_id=4, valid_sids=[3, 10], now=now)
        self.assertNotIn('3', ex)
        self.assertIn('10', ex)
        self.assertEqual(ex['10']['remaining_bytes'], 1_500_000_000_000)
        self.assertEqual(ex['10']['rule_name'], '备用节点双向月流量')

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cg = root / 'cgroup'
            pr = root / 'proc'
            (cg / 'thinksmart-intercom-tunnel.service').mkdir(parents=True)
            (cg / 'thinksmart-intercom-tunnel-alt.service').mkdir(parents=True)
            (cg / 'local-proxy.service').mkdir(parents=True)
            (cg / 'thinksmart-intercom-tunnel.service' / 'cgroup.procs').write_text('101\n102\n', encoding='utf-8')
            (cg / 'thinksmart-intercom-tunnel-alt.service' / 'cgroup.procs').write_text('201\n', encoding='utf-8')
            (cg / 'local-proxy.service' / 'cgroup.procs').write_text('301\n', encoding='utf-8')
            for pid, comm in ((101, 'bash'), (102, 'ssh'), (201, 'bash'), (301, 'proxy')):
                (pr / str(pid)).mkdir(parents=True)
                (pr / str(pid) / 'comm').write_text(f'{comm}\n', encoding='utf-8')
            (pr / 'net').mkdir(parents=True)
            (pr / 'net' / 'tcp').write_text(
                '  sl  local_address rem_address   st tx_queue rx_queue\n'
                '   0: 0100007F:0438 00000000:0000 0A 00000000:00000000\n',
                encoding='utf-8',
            )
            links_ok = MOD.probe_local_links(prefer_direct=False, cgroup_dir=cg, proc_dir=pr)
            by_id = {x['id']: x for x in links_ok}
            self.assertEqual(by_id['tun_main']['status'], 'ok')
            self.assertEqual(by_id['tun_main']['detail'], '已连通')
            self.assertEqual(by_id['tun_bak']['status'], 'warn')
            self.assertEqual(by_id['tun_bak']['detail'], '重连中')
            self.assertEqual(by_id['proxy']['status'], 'ok')
            self.assertIn('1080', by_id['proxy']['detail'])

            links_fb = MOD.probe_local_links(prefer_direct=True, cgroup_dir=cg, proc_dir=pr)
            self.assertEqual({x['id']: x for x in links_fb}['proxy']['detail'], '直连兜底')

    def test_round4_voice_compare_carrier_specific_and_ui_cmds(self):
        subcmd_re = re.compile(r'^[a-zA-Z0-9_-]{1,24}$')
        snap = {
            'connected': True,
            'age': 0.4,
            'servers': MOD.normalize(SAMPLE_WS, [3, 10]),
            'traffic': {'available': False, 'server_id': 3},
            'extra_traffic': {},
            'carriers': MOD.normalize_server_service(SAMPLE_SERVER_SVC_3),
            'unicom_map': {'3': {'ms': 162.2, 'status': 'ok'}, '10': {'ms': 78.4, 'status': 'ok'}},
            'isp_map': {
                '3': {
                    '联通': {'ms': 162.2, 'loss': 0.0, 'status': 'ok', 'stale': False},
                    '电信': {'ms': 157.6, 'loss': 0.0, 'status': 'ok', 'stale': False},
                    '移动': {'ms': 154.2, 'loss': 0.0, 'status': 'ok', 'stale': False},
                },
                '10': {
                    '联通': {'ms': 78.4, 'loss': 0.0, 'status': 'ok', 'stale': False},
                    '电信': {'ms': 212.0, 'loss': 3.3, 'status': 'warn', 'stale': False},
                    '移动': {'ms': 95.0, 'loss': 0.0, 'status': 'ok', 'stale': False},
                },
            },
            'local_links': [
                {'id': 'us_tunnel', 'name': '主隧道', 'status': 'ok', 'detail': '已连通'},
                {'id': 'jp_tunnel', 'name': '备用隧道', 'status': 'ok', 'detail': '已连通'},
                {'id': 'local_proxy', 'name': '本地代理', 'status': 'ok', 'detail': '正常 :1080'},
            ],
        }
        # Cross-server lowest latency (default Unicom vs Telecom)
        low_uni = MOD.build_voice_summary(snap, query='哪台服务器延迟最低')
        self.assertIn('联通延迟最低的是：备用节点 78 毫秒、主力节点 162 毫秒', low_uni)
        cmd_lat = MOD.resolve_voice_ui_cmd(snap, query='哪台服务器延迟最低', focus='compare')
        self.assertEqual(cmd_lat, 'fleet_lat')
        self.assertRegex(cmd_lat, subcmd_re)

        low_ct = MOD.build_voice_summary(snap, query='哪台电信最快')
        self.assertIn('电信延迟最低的是：主力节点 158 毫秒、备用节点 212 毫秒', low_ct)

        # Cross-server load & disk comparison
        hi_load = MOD.build_voice_summary(snap, query='哪台负载最高')
        self.assertIn('负载最高的服务器是：备用节点', hi_load)
        cmd_load = MOD.resolve_voice_ui_cmd(snap, query='哪台负载最高')
        self.assertEqual(cmd_load, 'fleet_load')
        self.assertRegex(cmd_load, subcmd_re)

        hi_disk = MOD.build_voice_summary(snap, query='哪台磁盘最满')
        self.assertIn('磁盘占用最高的是：主力节点', hi_disk)

        # Single-server specific carrier query + carrier-specific UI command
        vn_ct = MOD.build_voice_summary(snap, query='备用节点电信延迟多少')
        self.assertIn('备用节点当前电信延迟 212 毫秒（丢包 3.3%）', vn_ct)
        self.assertIn('同机联通 78 毫秒、移动 95 毫秒', vn_ct)
        for q_str, expected_cmd in (
            ('打开备用节点电信波形', 'detail_10_ct'),
            ('打开备用节点移动波形', 'detail_10_cm'),
            ('打开备用节点联通波形', 'detail_10_cu'),
            ('打开备用节点波形', 'detail_10'),
        ):
            c_val = MOD.resolve_voice_ui_cmd(snap, query=q_str)
            self.assertEqual(c_val, expected_cmd)
            self.assertRegex(c_val, subcmd_re)

        # Local tunnel health voice query
        lk_txt = MOD.build_voice_summary(snap, query='本机隧道和本地代理正常吗')
        self.assertIn('本机链路状态：主隧道 已连通，备用隧道 已连通', lk_txt)

    def test_round5_example_quota_reset_schedule_and_ledger_rollover(self):
        srv_cfg = json.loads(_asset('/usr/local/share/thinksmart-monitor/servers.example.json', 'monitor/servers.example.json').read_text(encoding='utf-8'))
        tr_cfg = json.loads(_asset('/usr/local/share/thinksmart-monitor/traffic.example.json', 'monitor/traffic.example.json').read_text(encoding='utf-8'))
        by_sid = {s['id']: s for s in srv_cfg}
        expected_meta = {
            1: ('1TB', 1),
            2: ('500GB', 15),
            3: ('2TB', 1),
        }
        for sid, (lim_str, rday) in expected_meta.items():
            self.assertEqual(by_sid[sid]['traffic_limit'], lim_str)
            self.assertEqual(by_sid[sid]['traffic_reset_day'], rday)
            self.assertTrue(by_sid[sid].get('traffic_limit_override'))

        self.assertEqual(tr_cfg['server_id'], 1)
        self.assertEqual(tr_cfg['reset_day'], 1)
        self.assertEqual(tr_cfg['limit_bytes'], 1_000_000_000_000)
        local_rules = tr_cfg['local_rules']
        self.assertEqual(local_rules['2']['reset_day'], 15)
        self.assertEqual(local_rules['2']['limit_bytes'], 500_000_000_000)
        self.assertEqual(local_rules['3']['mode'], 'max')

        # Billing cycle boundary math (including exact midnight rollover & Dec->Jan rollover)
        t_nov13 = dt.datetime.fromisoformat('2026-11-13T10:30:00+08:00')
        s15, e15 = MOD.compute_billing_cycle(15, t_nov13)
        self.assertEqual(s15.isoformat(), '2026-10-15T00:00:00+08:00')
        self.assertEqual(e15.isoformat(), '2026-11-15T00:00:00+08:00')
        s15_exact, e15_exact = MOD.compute_billing_cycle(15, dt.datetime.fromisoformat('2026-11-15T00:00:00+08:00'))
        self.assertEqual(s15_exact.isoformat(), '2026-11-15T00:00:00+08:00')
        self.assertEqual(e15_exact.isoformat(), '2026-12-15T00:00:00+08:00')
        s1_dec, e1_dec = MOD.compute_billing_cycle(1, dt.datetime.fromisoformat('2026-12-15T12:00:00+08:00'))
        self.assertEqual(s1_dec.isoformat(), '2026-12-01T00:00:00+08:00')
        self.assertEqual(e1_dec.isoformat(), '2027-01-01T00:00:00+08:00')

        # Monotonic ledger: override stale public_note, survive VPS reboot, ignore empty state, and roll over at 00:00
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'servers.json').write_text(json.dumps([by_sid[1], by_sid[2], by_sid[3]]), encoding='utf-8')
            (p / 'traffic.json').write_text(json.dumps(tr_cfg), encoding='utf-8')
            mon = MOD.Monitor(p, state_dir=p)
            mon.connected = True

            ts_mid = dt.datetime.fromisoformat('2026-11-13T12:00:00+08:00').timestamp()
            iso_mid = dt.datetime.fromtimestamp(ts_mid - 1, tz=dt.timezone.utc).isoformat()
            ws_frame1 = {
                'now': int(ts_mid * 1000),
                'servers': [
                    {
                        'id': 2, 'name': '亚太节点', 'last_active': iso_mid,
                        'public_note': json.dumps({'planDataMod': {'trafficVol': '800GB', 'trafficType': '2'}}),
                        'state': {'net_in_transfer': 3_000_000_000, 'net_out_transfer': 4_000_000_000, 'uptime': 86400, 'mem_used': 100_000_000},
                    },
                    {
                        'id': 3, 'name': '欧洲节点', 'last_active': iso_mid,
                        'public_note': json.dumps({'planDataMod': {'trafficVol': '2TB', 'trafficType': '1'}}),
                        'state': {'net_in_transfer': 2_000_000_000, 'net_out_transfer': 5_000_000_000, 'uptime': 86400, 'mem_used': 100_000_000},
                    },
                ],
            }
            with patch.object(MOD.time, 'time', return_value=ts_mid):
                mon.update(ws_frame1)
                snap1 = mon.snapshot()
            s2 = next(s for s in snap1['servers'] if s['id'] == 2)
            self.assertEqual(s2['traffic_limit'], '500GB')
            er2 = snap1['extra_traffic']['2']
            er3 = snap1['extra_traffic']['3']
            self.assertEqual(er2['used_bytes'], 7_000_000_000)
            self.assertFalse(er2['complete'])
            self.assertIsNone(er2['remaining_bytes'])
            self.assertEqual(er3['used_bytes'], 5_000_000_000)
            self.assertEqual((p / 'traffic_ledger.json').stat().st_mode & 0o777, 0o600)

            ws_glitch = {'now': int((ts_mid + 5) * 1000), 'servers': [{'id': 2, 'last_active': iso_mid, 'state': {}}]}
            with patch.object(MOD.time, 'time', return_value=ts_mid + 5):
                mon.update(ws_glitch)

            iso_reb = dt.datetime.fromtimestamp(ts_mid + 59, tz=dt.timezone.utc).isoformat()
            ws_reboot = {
                'now': int((ts_mid + 60) * 1000),
                'servers': [{
                    'id': 2, 'last_active': iso_reb,
                    'state': {'net_in_transfer': 500_000_000, 'net_out_transfer': 600_000_000, 'uptime': 120, 'mem_used': 100_000_000},
                }],
            }
            with patch.object(MOD.time, 'time', return_value=ts_mid + 60):
                mon.update(ws_reboot)
                snap_reb = mon.snapshot()
            self.assertEqual(snap_reb['extra_traffic']['2']['used_bytes'], 8_100_000_000)

            ts_pre = dt.datetime.fromisoformat('2026-11-14T23:55:00+08:00').timestamp()
            iso_pre = dt.datetime.fromtimestamp(ts_pre - 1, tz=dt.timezone.utc).isoformat()
            with patch.object(MOD.time, 'time', return_value=ts_pre):
                mon.update({
                    'now': int(ts_pre * 1000),
                    'servers': [{
                        'id': 2, 'last_active': iso_pre,
                        'state': {'net_in_transfer': 10_000_000_000, 'net_out_transfer': 10_000_000_000, 'uptime': 400000, 'mem_used': 100_000_000},
                    }],
                })
            ts_post = dt.datetime.fromisoformat('2026-11-15T00:05:00+08:00').timestamp()
            iso_post = dt.datetime.fromtimestamp(ts_post - 1, tz=dt.timezone.utc).isoformat()
            with patch.object(MOD.time, 'time', return_value=ts_post):
                mon.update({
                    'now': int(ts_post * 1000),
                    'servers': [{
                        'id': 2, 'last_active': iso_post,
                        'state': {'net_in_transfer': 11_000_000_000, 'net_out_transfer': 11_000_000_000, 'uptime': 400600, 'mem_used': 100_000_000},
                    }],
                })
                snap_post = mon.snapshot()
            er2_post = snap_post['extra_traffic']['2']
            self.assertTrue(er2_post['complete'])
            self.assertEqual(er2_post['from'], '2026-11-15T00:00:00+08:00')
            self.assertEqual(er2_post['to'], '2026-12-15T00:00:00+08:00')
            self.assertEqual(er2_post['used_bytes'], 1_000_000_000)
            self.assertEqual(er2_post['remaining_bytes'], 499_000_000_000)

            v_s2 = MOD.build_voice_summary(snap_post, query='亚太节点流量什么时候重置')
            self.assertIn('500 GB', v_s2)
            self.assertIn('每月15日', v_s2)
            v_all = MOD.build_voice_summary(snap_post, query='所有服务器流量和重置时间')
            self.assertIn('亚太节点 500 GB（每月15日重置', v_all)

class HttpEndpointsTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_latency_badge_cors_and_voice_endpoints(self):
        upstream_app = web.Application()
        upstream_app.router.add_get('/service', lambda _r: web.json_response(SAMPLE_SVC))
        upstream_app.router.add_get('/server/3/service', lambda _r: web.json_response(SAMPLE_SERVER_SVC_3))
        up_srv = TestServer(upstream_app)
        await up_srv.start_server()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td)
                (p / 'servers.json').write_text(
                    json.dumps([
                        {'id': 3, 'name': '主力节点', 'country': 'us', 'price': '$20', 'cycle': 'Year'},
                        {'id': 10, 'name': '备用节点', 'country': 'vn', 'price': '$30', 'cycle': 'Year'},
                    ]),
                    encoding='utf-8',
                )
                (p / 'traffic.json').write_text(json.dumps(TRAFFIC_CFG), encoding='utf-8')
                (p / 'index.html').write_text('ok', encoding='utf-8')
                (p / 'display.html').write_text('ok', encoding='utf-8')
                mon = MOD.Monitor(p)
                mon.update(SAMPLE_WS)
                mon.connected = True
                base = f'http://127.0.0.1:{up_srv.port}'
                with patch.object(MOD, 'UPSTREAM', 'ws://127.0.0.1:1/unused'), \
                     patch.object(MOD, 'TRAFFIC_UPSTREAM', f'{base}/service'), \
                     patch.object(MOD, 'SERVER_SERVICE_UPSTREAM', f'{base}/server/{{sid}}/service'), \
                     patch.dict(os.environ, {'NEZHA_PROXY': ''}):
                    client = TestClient(TestServer(mon.app()))
                    await client.start_server()
                    try:
                        r_st = await client.get('/api/state')
                        self.assertEqual(r_st.status, 200)
                        st_json = await r_st.json()
                        self.assertEqual([c['name'] for c in st_json['carriers']], ['联通', '电信', '移动'])
                        self.assertAlmostEqual(st_json['carriers'][0]['latest_ms'], 162.2)

                        r_lat = await client.get('/api/server_latency?id=3')
                        self.assertEqual(r_lat.status, 200)
                        lat_json = await r_lat.json()
                        self.assertTrue(lat_json['ok'])
                        self.assertEqual(lat_json['unicom']['name'], '联通')
                        self.assertAlmostEqual(lat_json['others']['电信'], 157.6)
                        self.assertAlmostEqual(lat_json['others']['移动'], 154.2)

                        r_unknown = await client.get('/api/server_latency?id=999')
                        self.assertEqual(r_unknown.status, 404)

                        r_badge = await client.get('/api/badge', headers={'Origin': 'http://127.0.0.1:8090'})
                        self.assertEqual(r_badge.status, 200)
                        self.assertEqual(r_badge.headers.get('Access-Control-Allow-Origin'), 'http://127.0.0.1:8090')

                        r_vs = await client.get('/api/voice_summary?focus=billing')
                        self.assertEqual(r_vs.status, 200)
                        vs_json = await r_vs.json()
                        self.assertIn('50 美元', vs_json['text'])

                        bad = await client.get('/api/voice_summary', headers={'Host': 'evil.example.com'})
                        self.assertEqual(bad.status, 403)
                    finally:
                        await client.close()
        finally:
            await up_srv.close()

    async def test_proxy_to_direct_fallback_and_voice_open_ui(self):
        upstream_app = web.Application()
        upstream_app.router.add_get('/service', lambda _r: web.json_response(SAMPLE_SVC))
        upstream_app.router.add_get('/server/3/service', lambda _r: web.json_response(SAMPLE_SERVER_SVC_3))
        upstream_app.router.add_get('/server/10/service', lambda _r: web.json_response(SAMPLE_SERVER_SVC_3))
        up_srv = TestServer(upstream_app)
        await up_srv.start_server()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td)
                (p / 'servers.json').write_text(
                    json.dumps([
                        {'id': 3, 'name': '主力节点', 'country': 'us'},
                        {'id': 10, 'name': '备用节点', 'country': 'vn'},
                    ]),
                    encoding='utf-8',
                )
                (p / 'traffic.json').write_text(json.dumps(TRAFFIC_CFG), encoding='utf-8')
                (p / 'index.html').write_text('ok', encoding='utf-8')
                (p / 'display.html').write_text('ok', encoding='utf-8')
                mon = MOD.Monitor(p)
                mon.update(SAMPLE_WS)
                mon.connected = True
                base = f'http://127.0.0.1:{up_srv.port}'
                # Point NEZHA_PROXY to a closed loopback port (127.0.0.1:1) -> must auto-fallback to direct!
                with patch.object(MOD, 'UPSTREAM', 'ws://127.0.0.1:1/unused'), \
                     patch.object(MOD, 'TRAFFIC_UPSTREAM', f'{base}/service'), \
                     patch.object(MOD, 'SERVER_SERVICE_UPSTREAM', f'{base}/server/{{sid}}/service'), \
                     patch.dict(os.environ, {'NEZHA_PROXY': 'http://127.0.0.1:1'}):
                    client = TestClient(TestServer(mon.app()))
                    await client.start_server()
                    try:
                        r_lat = await client.get('/api/server_latency?id=3')
                        self.assertEqual(r_lat.status, 200)
                        lat_json = await r_lat.json()
                        self.assertTrue(lat_json['ok'])
                        self.assertGreater(mon._prefer_direct_until, time.monotonic())

                        r_open = await client.get('/api/voice_summary?query=打开备用节点波形&open_ui=1')
                        self.assertEqual(r_open.status, 200)
                        open_json = await r_open.json()
                        self.assertTrue(open_json['open_ui'])
                        self.assertEqual(open_json['open_ui_cmd'], 'detail_10')

                        r_badge = await client.get('/api/badge')
                        b_json = await r_badge.json()
                        self.assertEqual(b_json['open_ui_seq'], 1)
                        self.assertTrue(b_json['open_ui_fresh'])
                        self.assertEqual(b_json['open_ui_cmd'], 'detail_10')
                    finally:
                        await client.close()
        finally:
            await up_srv.close()

    async def test_round4_idle_probe_debounce_and_voice_compare_http(self):
        ws_mode = {'mode': 'transient_blip'}

        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            now_s = time.time()
            now_ms = int(now_s * 1000)
            iso_live = dt.datetime.fromtimestamp(now_s - 2, tz=dt.timezone.utc).isoformat()
            iso_dead = dt.datetime.fromtimestamp(now_s - 360, tz=dt.timezone.utc).isoformat()
            first_10 = iso_live if ws_mode['mode'] == 'healthy' else iso_dead
            second_10 = iso_dead if ws_mode['mode'] == 'persistent_dead' else iso_live
            await ws.send_str(json.dumps({
                'now': now_ms,
                'servers': [{'id': 3, 'last_active': iso_live}, {'id': 10, 'last_active': first_10}],
            }))
            await asyncio.sleep(0.05)
            await ws.send_str(json.dumps({
                'now': now_ms + 50,
                'servers': [{'id': 3, 'last_active': iso_live}, {'id': 10, 'last_active': second_10}],
            }))
            await asyncio.sleep(0.35)
            await ws.close()
            return ws

        svc_10 = copy.deepcopy(SAMPLE_SERVER_SVC_3)
        for item in svc_10['data']:
            item['server_id'] = 10
            item['server_name'] = '备用节点'
            if item['monitor_name'] == '电信':
                item['avg_delay'][-1] = 88.0

        upstream_app = web.Application()
        upstream_app.router.add_get('/ws', ws_handler)
        upstream_app.router.add_get('/service', lambda _r: web.json_response(SAMPLE_SVC))
        upstream_app.router.add_get('/server/3/service', lambda _r: web.json_response(SAMPLE_SERVER_SVC_3))
        upstream_app.router.add_get('/server/10/service', lambda _r: web.json_response(svc_10))
        up_srv = TestServer(upstream_app)
        await up_srv.start_server()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td)
                (p / 'servers.json').write_text(
                    json.dumps([
                        {'id': 3, 'name': '主力节点', 'country': 'us'},
                        {'id': 10, 'name': '备用节点', 'country': 'vn'},
                    ]),
                    encoding='utf-8',
                )
                (p / 'traffic.json').write_text(json.dumps(TRAFFIC_CFG), encoding='utf-8')
                (p / 'index.html').write_text('ok', encoding='utf-8')
                (p / 'display.html').write_text('ok', encoding='utf-8')
                mon = MOD.Monitor(p, state_dir=p)
                base_http = f'http://127.0.0.1:{up_srv.port}'
                base_ws = f'ws://127.0.0.1:{up_srv.port}/ws'
                with patch.object(MOD, 'UPSTREAM', base_ws), \
                     patch.object(MOD, 'TRAFFIC_UPSTREAM', f'{base_http}/service'), \
                     patch.object(MOD, 'SERVER_SERVICE_UPSTREAM', f'{base_http}/server/{{sid}}/service'), \
                     patch.dict(os.environ, {'NEZHA_PROXY': ''}):
                    # 1. Transient single-frame blip must be filtered out by debounce recheck
                    await mon._probe_once_while_idle(recheck_delay=0.20)
                    self.assertEqual(mon.compute_badge()['text'], '')
                    self.assertTrue(all(s['alive'] is True for s in mon.servers))

                    # 2. Persistent offline across debounce window must trigger offline badge & event
                    ws_mode['mode'] = 'persistent_dead'
                    await mon._probe_once_while_idle(recheck_delay=0.15)
                    self.assertEqual(mon.compute_badge()['text'], '1离线')

                    # 3. Restore online and verify HTTP /api/voice_summary warms all servers on compare query
                    ws_mode['mode'] = 'healthy'
                    mon.update(SAMPLE_WS)
                    mon.connected = True
                    client = TestClient(TestServer(mon.app()))
                    await client.start_server()
                    try:
                        r_cmp = await client.get('/api/voice_summary?query=打开延迟最低的电信排行&focus=compare&open_ui=1')
                        self.assertEqual(r_cmp.status, 200)
                        cmp_json = await r_cmp.json()
                        self.assertEqual(cmp_json['open_ui_cmd'], 'fleet_lat')
                        self.assertIn('备用节点 88 毫秒', cmp_json['text'])
                        self.assertIn('主力节点 158 毫秒', cmp_json['text'])

                        r_ct = await client.get('/api/voice_summary?query=打开备用节点电信波形&open_ui=1')
                        ct_json = await r_ct.json()
                        self.assertEqual(ct_json['open_ui_cmd'], 'detail_10_ct')
                    finally:
                        await client.close()
        finally:
            await up_srv.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
