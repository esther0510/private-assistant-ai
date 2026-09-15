import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_ai_assistant.rhythm_diagnostic import (
    aggregate, analyze, read_samples, report_text, run_metrics, save_session, validate_metrics)


def make_run(setting, accuracy=98, sd=10, frames=None):
    return {"setting": setting, "chart": "song-hard", "environment": {
        "display_px": [1920, 1080] if setting == "A" else [1280, 720],
        "client_px": [1920, 1080] if setting == "A" else [1280, 720],
        "dpi_percent": 100, "refresh_hz": 144, "presentation": "windowed", "monitor": "DISPLAY1"},
        "metrics": {"accuracy": accuracy, "timing_sd_ms": sd, "timing_bias_ms": 0,
                    "miss": 2, "total_notes": 1000},
        "samples": {"frametime_ms": frames} if frames else {}}


def session(runs):
    return {"chart": "song-hard", "controls_confirmed": True, "runs": runs}


class RhythmAnalysisTests(unittest.TestCase):
    def test_ab_session_aggregation(self):
        runs = [make_run("A", x) for x in (98, 99, 100)] + [make_run("B", x) for x in (95, 96, 97)]
        groups = aggregate(runs)
        self.assertEqual(groups["A"]["count"], 3)
        self.assertEqual(groups["A"]["metrics"]["accuracy"]["mean"], 99)
        self.assertEqual(groups["B"]["metrics"]["accuracy"]["mean"], 96)
        self.assertEqual(groups["A"]["metrics"]["miss_rate"]["mean"], .2)

    def test_insufficient_never_formal(self):
        for n in (0, 1, 2):
            report = analyze(session([make_run("A")] * 3 + [make_run("B", 80)] * n))
            self.assertFalse(report["formal"])
            self.assertIn("暫時趨勢", report["verdict"])
            self.assertEqual(report["confidence"], 0)

    def test_missing_metrics_not_zero_or_formal(self):
        runs = [make_run(s) for s in ("A", "B") for _ in range(3)]
        for r in runs:
            r["metrics"] = {"frametime_p99_ms": 12}
        self.assertFalse(analyze(session(runs))["formal"])
        self.assertNotIn("accuracy", aggregate(runs)["A"]["metrics"])

    def test_spike_correlation_and_system_evidence(self):
        runs = [make_run("A", 99, frames=[7] * 100) for _ in range(3)]
        runs += [make_run("B", 96, frames=[7] * 90 + [35] * 10) for _ in range(3)]
        report = analyze(session(runs))
        self.assertTrue(report["formal"])
        self.assertIn("系統/顯示", report["verdict"])
        self.assertAlmostEqual(report["spike_correlation"], 1)
        self.assertGreater(report["confidence"], 60)
        self.assertIn("frametime_p99_ms", report_text(session(runs)))

    def test_spikes_without_result_change_not_system_cause(self):
        runs = [make_run("A", frames=[7] * 100)] * 3 + [make_run("B", frames=[35] * 100)] * 3
        self.assertNotIn("系統/顯示因素較可疑", analyze(session(runs))["verdict"])

    def test_different_resolution_same_performance(self):
        runs = [make_run(s) for s in ("A", "B") for _ in range(3)]
        report = analyze(session(runs))
        self.assertTrue(report["formal"])
        self.assertIn("非解析度主因", report["verdict"])
        self.assertIn("個人因素證據仍不足", report["verdict"])

    def test_high_within_setting_timing_sd(self):
        runs = [make_run(s, sd=35) for s in ("A", "B") for _ in range(3)]
        self.assertIn("打點穩定度/個人 offset 較可疑", analyze(session(runs))["verdict"])

    def test_noise_prevents_large_but_unstable_effect(self):
        runs = [make_run("A", x) for x in (80, 90, 100)] + [make_run("B", x) for x in (75, 85, 95)]
        self.assertNotIn("系統/顯示因素較可疑", analyze(session(runs))["verdict"])

    def test_uncontrolled_chart_and_environment_block_formal(self):
        original = session([make_run(s) for s in ("A", "B") for _ in range(3)])
        for kind in ("chart", "environment", "during_run", "controls"):
            data = copy.deepcopy(original)
            if kind == "chart":
                data["runs"][0]["chart"] = "other"
            elif kind == "environment":
                data["runs"][0]["environment"]["refresh_hz"] = 60
            elif kind == "during_run":
                data["runs"][0]["environment_changed"] = True
            else:
                data["controls_confirmed"] = False
            self.assertFalse(analyze(data)["formal"], kind)

    def test_timestamp_jitter_not_timing_sd(self):
        run = make_run("A")
        run["metrics"] = {}
        run["samples"] = {"keydown_timestamp_ms": [10, 110, 310]}
        metrics = run_metrics(run)
        self.assertGreater(metrics["interval_jitter_ms"], 0)
        self.assertNotIn("timing_sd_ms", metrics)

    def test_samples_offsets_and_p99(self):
        run = make_run("A")
        run["samples"] = {"offset_ms": [-10, 0, 10], "frametime_ms": [10] * 99 + [30]}
        metrics = run_metrics(run)
        self.assertEqual(metrics["timing_sd_ms"], 10)
        self.assertAlmostEqual(metrics["frametime_p99_ms"], 10.2)
        self.assertEqual(metrics["spike_rate"], 1)

    def test_csv_validation_and_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.csv"
            path.write_text("frametime_ms,offset_ms\n10,-2\n12,2\n", encoding="utf-8")
            self.assertEqual(read_samples(path)["offset_ms"], [-2, 2])
            for content in ("unknown\n1", "frametime_ms\nnan", "frametime_ms\n0", "keydown_timestamp_ms\n2\n1"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_samples(path)
            destination = Path(folder) / "nested" / "session.json"
            data = session([make_run("A")])
            save_session(destination, data)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), data)

    def test_invalid_manual_metrics(self):
        for metrics in ({"accuracy": 101}, {"miss": -1}, {"timing_sd_ms": float("nan")},
                        {"miss": 4, "total_notes": 3}, {"perfect": 1.5}):
            with self.assertRaises(ValueError):
                validate_metrics(metrics)


if __name__ == "__main__":
    unittest.main()
