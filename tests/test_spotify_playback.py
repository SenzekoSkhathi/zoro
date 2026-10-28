import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import zoro_spotify


class SearchAndPlayTests(unittest.TestCase):
    def setUp(self):
        self.sp = Mock()
        self.sp.search.return_value = {
            "tracks": {"items": [{"id": "wanted", "uri": "spotify:track:wanted",
                                  "name": "Wanted", "artists": [{"name": "Artist"}]}]}
        }
        self.sp.devices.return_value = {"devices": [{"id": "device", "is_active": True}]}
        self.client = patch.object(zoro_spotify, "_client", return_value=self.sp)
        self.sleep = patch.object(zoro_spotify.time, "sleep")
        self.client.start()
        self.sleep.start()
        self.addCleanup(self.client.stop)
        self.addCleanup(self.sleep.stop)

    def state(self, track_id, playing=True):
        return {"is_playing": playing,
                "item": {"id": track_id, "uri": f"spotify:track:{track_id}",
                         "name": track_id}, "device": {"id": "device"}}

    def test_confirmed_playback(self):
        self.sp.current_playback.return_value = self.state("wanted")
        result = zoro_spotify.search_and_play("Wanted")
        self.assertEqual(result["status"], "playing")
        self.assertEqual(self.sp.current_playback.call_count, 2)
        self.sp.start_playback.assert_called_once_with(
            device_id="device", uris=["spotify:track:wanted"])

    def test_delayed_playback(self):
        self.sp.current_playback.side_effect = [None, self.state("old"),
                                                self.state("wanted"), self.state("wanted")]
        self.assertEqual(zoro_spotify.search_and_play("Wanted")["status"], "playing")
        self.assertEqual(self.sp.current_playback.call_count, 4)

    def test_accepted_then_stops(self):
        self.sp.current_playback.side_effect = [self.state("wanted"), None, None, None, None]
        result = zoro_spotify.search_and_play("Wanted")
        self.assertEqual(result["status"], "unverified")
        self.assertIn("no active track", result["detail"])

    def test_no_active_playback(self):
        self.sp.current_playback.return_value = None
        result = zoro_spotify.search_and_play("Wanted")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(self.sp.current_playback.call_count, 5)

    def test_wrong_track(self):
        self.sp.current_playback.return_value = self.state("other")
        result = zoro_spotify.search_and_play("Wanted")
        self.assertEqual(result["status"], "unverified")
        self.assertIn("other", result["detail"])

    def test_paused_target_is_not_playing(self):
        self.sp.current_playback.return_value = self.state("wanted", playing=False)
        result = zoro_spotify.search_and_play("Wanted")
        self.assertEqual(result["status"], "unverified")
        self.assertIn("paused", result["detail"])

    def test_transient_state(self):
        self.sp.current_playback.side_effect = [self.state("wanted"),
                                                self.state("wanted", playing=False),
                                                None, None, None]
        self.assertEqual(zoro_spotify.search_and_play("Wanted")["status"], "unverified")

    def test_wrong_device(self):
        state = self.state("wanted")
        state["device"]["id"] = "other-device"
        self.sp.current_playback.return_value = state
        result = zoro_spotify.search_and_play("Wanted")
        self.assertEqual(result["status"], "unverified")
        self.assertIn("other-device", result["detail"])

    def test_spotify_verification_error_propagates(self):
        from spotipy.exceptions import SpotifyException
        self.sp.current_playback.side_effect = SpotifyException(403, -1, "PREMIUM_REQUIRED")
        result = zoro_spotify.search_and_play("Wanted")
        self.assertEqual(result["status"], "unverified")
        self.assertIn("Could not verify", result["detail"])


if __name__ == "__main__":
    unittest.main()
