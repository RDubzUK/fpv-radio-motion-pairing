"""Exercise corrected tracking on a small clip with a changing tip appearance."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from fpv_matcher.config import validate_calibration
from fpv_matcher.radio.derivation import cached_sticks, cache_path, cache_status
from fpv_matcher.radio.landmarks import from_annotations, landmark_frames, validate_landmarks
from fpv_matcher.radio.stick_tracker import iter_sticks


def calibration():
    return validate_calibration({"stickcam_mode": "fixed", "reference_time": 0,
        **{side: {"x": x, "y": 40, "width": 80, "height": 80, "center_x": 40, "center_y": 40,
                  "travel_x": 35, "travel_y": 35, "tip_radius": 6, "tip_x": 40, "tip_y": 40}
           for side, x in (("left_gimbal", 40), ("right_gimbal", 190))}})


class LandmarkTimelineTests(unittest.TestCase):
    def test_off_grid_frames_forward_and_backward(self):
        points = [{"time": .15, "left_tip": [.2, .4]}, {"time": .2, "right_tip": [.6, .4]}]
        for reverse in (False, True):
            decoded = []
            def decode(at):
                decoded.append(at)
                return f"marked {at}"
            grid = [(at, f"grid {at}") for at in (0., .1, .2, .3)]
            if reverse:
                grid.reverse()
            result = list(landmark_frames(iter(grid), points, decode, reverse))
            self.assertEqual([row[0] for row in result], sorted([0., .1, .15, .2, .3], reverse=reverse))
            self.assertEqual(decoded, [.15])
            self.assertIn((.2, "grid 0.2"), result)

    def test_controls_and_notes_do_not_become_landmarks(self):
        self.assertEqual(from_annotations([{"time": 1, "controls": {"roll": 1}, "note": "test"}]), [])
        for point in ({"time": True, "left_tip": [.1, .2]}, {"time": 1, "left_tip": [float('nan'), .2]},
                      {"time": 3, "left_tip": [.1, .2]}):
            with self.assertRaises(ValueError):
                validate_landmarks([point], duration=3)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CorrectedTrackingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "appearance-change.mkv"
        rng = np.random.default_rng(47)
        first, changed, right = [rng.integers(30, 240, (13, 13), dtype=np.uint8) for _ in range(3)]
        frames = []
        for i in range(60):
            frame = np.full((200, 320), 25, np.uint8)
            tip, (x, y) = (first, (80, 80)) if i < 20 else (changed, (100, 65))
            frame[y-6:y+7, x-6:x+7] = tip
            frame[74:87, 224:237] = right
            frames.append(frame)
        cls.frames = frames
        subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray",
                        "-s", "320x200", "-r", "20", "-i", "pipe:0", "-c:v", "ffv1", str(cls.source)],
                       input=b"".join(frame.tobytes() for frame in frames), check=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.env = patch.dict(os.environ, {"FPV_MATCHER_TRACKING": "cpu", "FPV_MATCHER_DECODE": "cpu"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cache = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(shutil.rmtree, self.cache)

    def test_tip_landmark_refreshes_appearance_and_cache_reuses(self):
        base = calibration()
        original = cached_sticks(self.source, self.cache, base, 10)
        original_path = cache_path(self.source, self.cache, base, 10)
        late = (original["time"] >= 1.2) & (original["time"] < 2.5)
        self.assertTrue(np.all(original["yaw_quality"][late] == 0))
        corrected = {**base, "tracking_landmarks": [{"time": 1.15, "left_tip": [100/320, 65/200]}]}
        data = cached_sticks(self.source, self.cache, corrected, 10)
        self.assertNotEqual(original_path, cache_path(self.source, self.cache, corrected, 10))
        self.assertTrue(original_path.exists())
        anchor = np.flatnonzero(np.isclose(data["time"], 1.15))[0]
        self.assertEqual(data["left_tip_landmark"][anchor], 1)
        self.assertAlmostEqual(data["yaw"][anchor], 20/35, places=4)
        self.assertAlmostEqual(data["throttle"][anchor], 15/35, places=4)
        self.assertGreater(data["yaw_quality"][np.flatnonzero(np.isclose(data["time"], 1.2))[0]], .5)
        self.assertEqual(cache_status(self.source, self.cache, corrected, 10)["landmarks_applied"], 1)
        with patch("fpv_matcher.radio.derivation.extract_sticks", side_effect=AssertionError("must reuse saved trace")):
            np.testing.assert_array_equal(cached_sticks(self.source, self.cache, corrected, 10)["yaw"], data["yaw"])

    def test_full_frame_landmark_corrects_pose_and_neutral_center(self):
        base = calibration()
        snapshot = copy.deepcopy(base)
        snapshot["left_gimbal"].update(x=60, y=30, tip_x=40, tip_y=35, center_y=35)
        corrected = {**base, "tracking_landmarks": [{"time": 1.15, "calibration": snapshot}]}
        data = cached_sticks(self.source, self.cache, corrected, 10)
        anchor = np.flatnonzero(np.isclose(data["time"], 1.15))[0]
        self.assertEqual(data["frame_calibration_landmark"][anchor], 1)
        self.assertAlmostEqual(data["yaw"][anchor], 0, places=4)
        self.assertAlmostEqual(data["throttle"][anchor], 0, places=4)
        self.assertAlmostEqual(data["left_gimbal_0_x"][anchor], 60)
        self.assertAlmostEqual(data["left_tip_x"][anchor], 100)

    def test_tip_alone_cannot_manufacture_a_lost_radio_pose(self):
        base = calibration()
        base.update(stickcam_mode="wonky", radio_outline=[[20,20], [300,20], [300,160], [20,160]])
        tip_only = {**base, "tracking_landmarks": [{"time": 1.15, "left_tip": [100/320,65/200]}]}
        data = cached_sticks(self.source, self.cache, tip_only, 10)
        anchor = np.flatnonzero(np.isclose(data["time"], 1.15))[0]
        self.assertEqual(data["left_tip_landmark"][anchor], -1)
        self.assertEqual(data["yaw_quality"][anchor], 0)

    def test_inverted_mode_four_keeps_physical_sides_and_signs(self):
        base = calibration()
        base["left_gimbal"], base["right_gimbal"] = base["right_gimbal"], base["left_gimbal"]
        base.update(stick_mode=4, radio_rotation_deg=180, signs={"pitch": -1},
                    tracking_landmarks=[{"time": 0, "left_tip": [250/320,90/200], "right_tip": [65/320,65/200]}])
        row = next(iter_sticks(str(self.source), 10, base, _frames=iter([(0, self.frames[0])])))[1]
        self.assertAlmostEqual(row["roll"], -20/35, places=4)
        self.assertAlmostEqual(row["throttle"], 10/35, places=4)
        self.assertAlmostEqual(row["yaw"], 15/35, places=4)
        self.assertAlmostEqual(row["pitch"], 15/35, places=4)

    def test_review_api_activates_landmarks_preserves_baseline_and_guards_source(self):
        from fastapi import HTTPException
        from fpv_matcher import jobs, web
        with patch.object(jobs, "DATA", self.cache):
            job = web.create_job({"source": "local", "fpv_path": str(self.source), "radio_path": str(self.source)})
            identifier, name = job["id"], self.source.name
            base = web.save_calibration(identifier, name, calibration())["config"]
            cached_sticks(self.source, self.cache / "cache", base, 30)
            notes = [{"time": 1.15, "left_tip": [100/320,65/200], "controls": {"roll": .5}, "note": "changed tip appearance"}]
            saved = web.save_stick_tracking_review(identifier, name, {"annotations": notes})
            self.assertTrue(saved["landmarks_changed"])
            before = web.get_stick_tracking_review(identifier, name)
            self.assertIsNone(before["movement"])
            self.assertTrue(before["landmark_diagnostics"]["before"])
            cal = jobs.get(identifier)["calibrations"][name]
            self.assertNotIn("controls", cal["tracking_landmarks"][0])
            cached_sticks(self.source, self.cache / "cache", cal, 30)
            review = web.get_stick_tracking_review(identifier, name)
            self.assertEqual(review["movement"]["tracking"]["source"], "stored")
            self.assertEqual(review["movement"]["landmark_events"][0]["left_tip"], "applied")
            self.assertLess(review["landmark_diagnostics"]["current"][0]["tips"]["left"]["error_pixels"], .01)
            window = web.stick_tracking_window(identifier, name, start=1, duration=1)
            self.assertGreaterEqual(len(window["time"]), 30)
            notes[0]["controls"]["roll"] = -.5
            self.assertFalse(web.save_stick_tracking_review(identifier, name, {"annotations": notes})["landmarks_changed"])
            jobs.update(identifier, lambda current: current.update(status="running"))
            changed = copy.deepcopy(notes)
            changed[0]["left_tip"][0] += .01
            with self.assertRaises(HTTPException) as active:
                web.save_stick_tracking_review(identifier, name, {"annotations": changed})
            self.assertEqual(active.exception.status_code, 409)
            jobs.update(identifier, lambda current: current.update(status="draft"))
            stat = self.source.stat()
            try:
                os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
                with self.assertRaises(HTTPException) as stale:
                    web.save_stick_tracking_review(identifier, name, {"annotations": notes})
                self.assertEqual(stale.exception.status_code, 409)
            finally:
                os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
