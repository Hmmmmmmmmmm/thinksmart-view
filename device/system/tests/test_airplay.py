import importlib.util
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SOURCE = Path(__file__).resolve().parents[1] / "thinksmart-airplay.py"
SPEC = importlib.util.spec_from_file_location("thinksmart_airplay", SOURCE)
airplay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(airplay)


class AirPlaySessionTests(unittest.TestCase):
    def test_audio_flag_follows_last_control_disconnect_at_default_log_level(self):
        with tempfile.TemporaryDirectory() as temp:
            mirror_flag = Path(temp) / "uxplay-mirroring-active"
            audio_flag = mirror_flag.with_name("uxplay-audio-active")
            audio_flag.touch()

            proc = MagicMock(pid=1234)
            proc.poll.return_value = 0
            proc.wait.return_value = 0

            def receiver_output():
                self.assertFalse(audio_flag.exists(), "startup should clear a stale flag")
                yield "Accepted IPv4 client on socket 18, port 39157\n"
                yield "raop_rtp starting audio\n"
                self.assertTrue(audio_flag.exists())
                yield "Accepted IPv6 client on socket 20, port 39157\n"
                yield "Connection closed on socket 20\n"
                self.assertTrue(audio_flag.exists(), "a probe should not end playback")
                yield "Connection closed on socket 18\n"
                self.assertFalse(audio_flag.exists(), "the final disconnect should clear the flag")
                handle_signal = signal_setup.call_args_list[0].args[1]
                handle_signal(signal.SIGTERM, None)

            proc.stdout.__iter__.side_effect = receiver_output
            with (
                patch.object(airplay, "MIRROR_FLAG_PATH", mirror_flag),
                patch.object(airplay, "set_webkit_paused"),
                patch.object(airplay, "set_hardware_turbo"),
                patch.object(airplay, "pause_conflicting_media"),
                patch.object(airplay, "wait_for_wayland"),
                patch.object(airplay, "apply_system_tuning"),
                patch.object(airplay, "start_wayland_watchdog"),
                patch.object(airplay, "boost_process_priority"),
                patch.object(airplay.signal, "signal") as signal_setup,
                patch.object(airplay.subprocess, "Popen", return_value=proc),
            ):
                self.assertEqual(airplay.main(), 0)


if __name__ == "__main__":
    unittest.main()
