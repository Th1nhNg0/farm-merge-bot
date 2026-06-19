import random
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np


fake_pyautogui = types.ModuleType("pyautogui")
fake_pyautogui.FAILSAFE = True
fake_pyautogui.PAUSE = 0.0
sys.modules.setdefault("pyautogui", fake_pyautogui)

import main  # noqa: E402


def apply_swaps(labels, swaps):
    result = labels[:]

    for swap in swaps:
        source = swap["from_slot"]
        destination = swap["to_slot"]
        result[source], result[destination] = result[destination], result[source]

    return result


class SwapPlanningTests(unittest.TestCase):
    def make_isometric_slots(self, labels, points):
        return [
            types.SimpleNamespace(
                label=label,
                center=point,
                screen_center=point,
                grid_anchor=point,
                w=20,
                h=20,
            )
            for label, point in zip(labels, points)
        ]

    def test_isometric_adjacency_excludes_logical_diagonal(self):
        slots = self.make_isometric_slots(
            ["a", "b", "c", "d"],
            [(0, 0), (40, 20), (-40, 20), (0, 40)],
        )

        adjacency = main.build_isometric_adjacency(slots)

        self.assertEqual({1, 2}, adjacency[0])
        self.assertNotIn(3, adjacency[0])

    def test_largest_isometric_component_excludes_distant_match(self):
        slots = self.make_isometric_slots(
            ["a", "a", "b", "b"],
            [(0, 0), (40, 20), (-40, 20), (0, 40)],
        )
        noise = types.SimpleNamespace(
            label="a",
            center=(500, 500),
            screen_center=(500, 500),
            grid_anchor=(500, 500),
            w=20,
            h=20,
        )

        kept = main.largest_orthogonal_component(slots + [noise])

        self.assertEqual(slots, kept)

    def test_template_paths_include_only_base_and_underscore_variants(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)

            for name in ("bo1.png", "bo1_2.png", "bo10.png", "bo2.png"):
                (directory / name).touch()

            with mock.patch.object(main, "TEMPLATE_DIR", directory):
                paths = main.template_paths("bo", 1)

        self.assertEqual(["bo1.png", "bo1_2.png"], [path.name for path in paths])

    def test_template_paths_allow_variant_only_template_sets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)

            for name in ("bo1_1.png", "bo1_2.png", "bo2_1.png"):
                (directory / name).touch()

            with mock.patch.object(main, "TEMPLATE_DIR", directory):
                paths = main.template_paths("bo", 1)

        self.assertEqual(["bo1_1.png", "bo1_2.png"], [path.name for path in paths])

    def test_center_deduplication_keeps_adjacent_overlapping_items(self):
        detections = [
            main.Detection("a_1", "a", 1, 0, 0, 40, 40, 0.90),
            main.Detection("b_1", "b", 1, 2, 1, 40, 40, 0.95),
            main.Detection("c_1", "c", 1, 25, 0, 40, 40, 0.85),
        ]

        kept = main.deduplicate_detections(detections)

        self.assertEqual(["b_1", "c_1"], [d.label for d in kept])

    def test_multiscale_detection_finds_a_scaled_template(self):
        rng = np.random.default_rng(7)
        template = rng.integers(0, 256, size=(20, 24, 3), dtype=np.uint8)
        scaled = cv2.resize(template, (26, 22), interpolation=cv2.INTER_CUBIC)
        screenshot = np.zeros((80, 100, 3), dtype=np.uint8)
        screenshot[30:52, 40:66] = scaled

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            cv2.imwrite(str(directory / "test1.png"), template)

            with (
                mock.patch.object(main, "TEMPLATE_DIR", directory),
                mock.patch.object(main, "TEMPLATE_SCALES", (1.0, 1.1)),
                mock.patch.object(main, "THRESHOLD", 0.75),
                mock.patch.object(main, "items", ["test"]),
                mock.patch.object(main, "levels", [1]),
                mock.patch.object(main, "item_levels", {}),
            ):
                diagnostics = {}
                detections = main.detect_all_items(
                    screenshot,
                    diagnostics=diagnostics,
                )

        best = max(detections, key=lambda detection: detection.score)
        self.assertEqual("test_1", best.label)
        self.assertEqual((53, 41), best.center)
        self.assertGreater(diagnostics["test_1"]["best_score"], 0.75)
        self.assertEqual(1, diagnostics["test_1"]["detected_count"])
        self.assertEqual(
            (26, 22),
            (
                diagnostics["test_1"]["best_width"],
                diagnostics["test_1"]["best_height"],
            ),
        )

    def test_detection_screen_click_position_is_rectangle_center(self):
        detection = main.Detection("bo_1", "bo", 1, 10, 20, 30, 40, 0.9)

        self.assertEqual((25, 40), detection.screen_center)

    def test_find_game_region_ignores_surrounding_desktop_panels(self):
        screenshot = np.zeros((500, 900, 3), dtype=np.uint8)
        screenshot[70:430, 220:710] = (40, 180, 80)
        screenshot[100:400, 20:160] = (50, 50, 50)

        with mock.patch.object(main, "GAME_CROP_PADDING", 0):
            region = main.find_game_region(screenshot)

        self.assertEqual((220, 70, 490, 360), region)

    def test_find_game_region_rejects_desktop_without_game(self):
        screenshot = np.full((500, 900, 3), 25, dtype=np.uint8)

        self.assertIsNone(main.find_game_region(screenshot))

    def test_cropped_detection_keeps_absolute_screen_coordinates(self):
        rng = np.random.default_rng(11)
        template = rng.integers(0, 256, size=(18, 22, 3), dtype=np.uint8)
        crop = np.zeros((80, 100, 3), dtype=np.uint8)
        crop[30:48, 40:62] = template

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            cv2.imwrite(str(directory / "test1.png"), template)

            with (
                mock.patch.object(main, "TEMPLATE_DIR", directory),
                mock.patch.object(main, "THRESHOLD", 0.70),
                mock.patch.object(main, "items", ["test"]),
                mock.patch.object(main, "levels", [1]),
                mock.patch.object(main, "item_levels", {}),
            ):
                detections = main.detect_all_items(crop, offset=(200, 70))

        best = max(detections, key=lambda detection: detection.score)
        self.assertEqual((251, 109), best.screen_center)

    def test_main_always_writes_debug_output(self):
        screenshot = np.zeros((80, 100, 3), dtype=np.uint8)

        with tempfile.TemporaryDirectory() as temp_dir:
            debug_dir = Path(temp_dir)

            with (
                mock.patch.object(
                    main,
                    "capture_game_bgr",
                    return_value=(screenshot, (0, 0)),
                ),
                mock.patch.object(main, "detect_all_items", return_value=[]),
                mock.patch.object(main, "DETECTION_DEBUG_DIR", debug_dir),
                mock.patch("builtins.print"),
            ):
                main.main()

            self.assertTrue((debug_dir / "board.png").is_file())
            self.assertTrue((debug_dir / "detections.png").is_file())
            self.assertTrue((debug_dir / "scores.csv").is_file())

    def test_three_cycle_uses_two_swaps(self):
        current = ["a", "b", "c"]
        target = ["b", "c", "a"]
        dist = np.array(
            [
                [0.0, 1.0, 3.0],
                [1.0, 0.0, 1.0],
                [3.0, 1.0, 0.0],
            ]
        )

        swaps = main.plan_swaps(current, target, dist)

        self.assertEqual(2, len(swaps))
        self.assertEqual(target, apply_swaps(current, swaps))
        self.assertEqual(
            [(1, 0), (2, 1)],
            [(swap["from_slot"], swap["to_slot"]) for swap in swaps],
        )

    def test_duplicate_reciprocal_pairs_choose_lowest_total_distance(self):
        current = ["a", "a", "b", "b"]
        target = ["b", "b", "a", "a"]
        dist = np.array(
            [
                [0.0, 9.0, 1.0, 8.0],
                [9.0, 0.0, 8.0, 1.0],
                [1.0, 8.0, 0.0, 9.0],
                [8.0, 1.0, 9.0, 0.0],
            ]
        )

        swaps = main.plan_swaps(current, target, dist)

        self.assertEqual(target, apply_swaps(current, swaps))
        self.assertEqual(
            {frozenset((0, 2)), frozenset((1, 3))},
            {
                frozenset((swap["from_slot"], swap["to_slot"]))
                for swap in swaps
            },
        )

    def test_random_duplicate_label_permutations_reach_target(self):
        rng = random.Random(20260619)

        for _ in range(300):
            size = rng.randint(2, 25)
            current = [rng.choice("abcde") for _ in range(size)]
            target = current[:]
            rng.shuffle(target)
            points = np.array(
                [(rng.random() * 100, rng.random() * 100) for _ in range(size)]
            )
            dist = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)

            swaps = main.plan_swaps(current, target, dist)

            self.assertEqual(target, apply_swaps(current, swaps))
            initially_correct = {
                i
                for i, labels in enumerate(zip(current, target))
                if labels[0] == labels[1]
            }
            touched = {
                slot
                for swap in swaps
                for slot in (swap["from_slot"], swap["to_slot"])
            }
            self.assertTrue(initially_correct.isdisjoint(touched))

    def test_rejects_different_label_counts(self):
        with self.assertRaisesRegex(ValueError, "same items"):
            main.plan_swaps(["a", "b"], ["a", "a"], np.zeros((2, 2)))

    def test_execute_swaps_has_no_limit(self):
        slots = [types.SimpleNamespace(screen_center=(i, i)) for i in range(102)]
        swaps = [
            {
                "from_slot": i + 1,
                "to_slot": i,
                "moving_label": "a",
                "replaced_label": "b",
            }
            for i in range(101)
        ]

        with (
            mock.patch.object(main, "drag_swap") as drag_swap,
            mock.patch.object(main.time, "sleep"),
            mock.patch("builtins.print"),
        ):
            main.execute_swaps(slots, swaps)

        self.assertEqual(101, drag_swap.call_count)


if __name__ == "__main__":
    unittest.main()
