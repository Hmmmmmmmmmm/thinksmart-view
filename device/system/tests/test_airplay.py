from contextlib import ExitStack
import importlib.util
from pathlib import Path
import signal
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SOURCE = Path(__file__).resolve().parents[1] / "thinksmart-airplay.py"
SPEC = importlib.util.spec_from_file_location("thinksmart_airplay", SOURCE)
airplay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(airplay)


class AirPlaySessionTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        runtime = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.mirror_flag = runtime / "uxplay-mirroring-active"
        self.audio_flag = runtime / "uxplay-audio-active"
        for name, path in {
            "MIRROR_FLAG_PATH": self.mirror_flag,
            "HINT_PATH": runtime / "screen.json",
            "BACKLIGHT_PATH": runtime / "backlight",
            "INTERCOM_FLAG_PATH": runtime / "intercom-call-active",
        }.items():
            stack.enter_context(patch.object(airplay, name, path))
        for name in (
            "set_webkit_paused", "set_hardware_turbo", "pause_conflicting_media",
            "wait_for_wayland", "apply_system_tuning", "start_wayland_watchdog",
            "boost_process_priority",
        ):
            stack.enter_context(patch.object(airplay, name))

    def run_receiver(self, events, *, direct_video):
        proc = MagicMock(pid=1234)
        proc.poll.return_value = 0
        proc.wait.return_value = 0

        def receiver_output():
            for line in events():
                yield line + "\n"
            handle_signal = signal_setup.call_args_list[0].args[1]
            handle_signal(signal.SIGTERM, None)

        proc.stdout.__iter__.side_effect = receiver_output
        with (
            patch.object(airplay, "direct_video_enabled", return_value=direct_video),
            patch.object(airplay.signal, "signal") as signal_setup,
            patch.object(airplay.subprocess, "Popen", side_effect=[proc]) as launch,
        ):
            self.assertEqual(airplay.main(), 0)
        launch.assert_called_once()
        return launch.call_args.args[0]

    def test_audio_flag_follows_last_control_disconnect_at_default_log_level(self):
        self.audio_flag.touch()

        def events():
            self.assertFalse(self.audio_flag.exists())
            yield "Accepted IPv4 client on socket 18, port 39157"
            yield "raop_rtp starting audio"
            self.assertTrue(self.audio_flag.exists())
            yield "Accepted IPv6 client on socket 20, port 39157"
            yield "Connection closed on socket 20"
            self.assertTrue(self.audio_flag.exists())
            yield "Connection closed on socket 18"
            self.assertFalse(self.audio_flag.exists())

        self.assertNotIn("-hls", self.run_receiver(events, direct_video=False))

    def test_mirroring_can_switch_to_direct_video_and_play_again(self):
        def events():
            yield "Accepted IPv4 client on socket 18, port 39157"
            yield "raop_rtp_mirror starting mirroring"
            self.assertTrue(self.mirror_flag.exists())
            yield "raop_rtp_mirror tcp socket is closed"
            self.assertFalse(self.mirror_flag.exists())
            yield "Accepted IPv4 client on socket 20, port 39157"
            yield "Will use GStreamer playbin version 3 to play HLS streamed video"
            self.assertTrue(self.mirror_flag.exists())
            yield "Connection closed on socket 18"
            self.assertTrue(self.mirror_flag.exists())
            yield "hls video has sent EOS"
            self.assertFalse(self.mirror_flag.exists())
            yield "Will use GStreamer playbin version 3 to play HLS streamed video"
            self.assertTrue(self.mirror_flag.exists())
            yield "**************************on_video_stop"
            self.assertFalse(self.mirror_flag.exists())

        command = self.run_receiver(events, direct_video=True)
        self.assertIn("-hls", command)
        self.assertEqual(command[command.index("-as") + 1], "pulsesink")
        self.assertTrue(command[command.index("-vs") + 1].startswith("waylandsink"))

    def test_direct_video_requires_an_uxplay_build_that_advertises_hls(self):
        help_result = CompletedProcess([], 0, "-hls [v]  Support HTTP Live Streaming\n")
        with patch.dict(airplay.os.environ, {}, clear=True), patch.object(
            airplay.subprocess, "run", return_value=help_result
        ):
            self.assertTrue(airplay.direct_video_enabled())
            help_result.stdout = "-vs 0  Streamed audio only\n"
            self.assertFalse(airplay.direct_video_enabled())

    def test_direct_video_can_be_disabled_in_service_settings(self):
        with patch.dict(airplay.os.environ, {"THINKSMART_AIRPLAY_DIRECT_VIDEO": "0"}), patch.object(
            airplay.subprocess, "run"
        ) as probe:
            self.assertFalse(airplay.direct_video_enabled())
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
