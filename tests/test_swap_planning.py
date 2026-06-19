import itertools
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
    def make_grid_slots(self, labels, columns):
        return [
            types.SimpleNamespace(
                label=label,
                center=((index % columns) * 40, (index // columns) * 40),
                w=20,
                h=20,
            )
            for index, label in enumerate(labels)
        ]

    def test_orthogonal_adjacency_excludes_diagonals(self):
        slots = self.make_grid_slots(["a", "b", "c", "d"], columns=2)

        adjacency = main.build_orthogonal_adjacency(slots)

        self.assertEqual({1, 2}, adjacency[0])
        self.assertNotIn(3, adjacency[0])

    def test_orthogonal_adjacency_does_not_bridge_a_missing_cell(self):
        centers = [(0, 0), (40, 0), (0, 40), (40, 40), (120, 0)]
        slots = [
            types.SimpleNamespace(label="a", center=center, w=20, h=20)
            for center in centers
        ]

        adjacency = main.build_orthogonal_adjacency(slots)

        self.assertNotIn(4, adjacency[1])
        self.assertEqual(set(), adjacency[4])

    def test_orthogonal_optimizer_uses_global_minimum_swaps_on_small_grid(self):
        current = ["a", "b", "a", "c", "b", "c"]
        slots = self.make_grid_slots(current, columns=3)
        dist = main.pairwise_distance_matrix(slots)
        adjacency = main.build_orthogonal_adjacency(slots)
        possible_targets = {
            target
            for target in set(itertools.permutations(current))
            if main.labels_are_orthogonally_connected(target, adjacency)
        }
        global_minimum = min(
            len(main.plan_swaps(slots, current, list(target), dist))
            for target in possible_targets
        )

        target, swaps, planned_adjacency = main.optimize_orthogonal_plan(slots)

        self.assertEqual(global_minimum, len(swaps))
        self.assertTrue(
            main.labels_are_orthogonally_connected(target, planned_adjacency)
        )
        self.assertEqual(target, apply_swaps(current, swaps))

    def test_orthogonal_optimizer_handles_grid_without_full_scan_path(self):
        centers = [(40, 0), (0, 40), (40, 40), (80, 40), (40, 80)]
        labels = ["b", "a", "a", "a", "a"]
        slots = [
            types.SimpleNamespace(label=label, center=center, w=20, h=20)
            for label, center in zip(labels, centers)
        ]
        adjacency = main.build_orthogonal_adjacency(slots)

        self.assertFalse(
            any(
                all(right in adjacency[left] for left, right in zip(order, order[1:]))
                for order in main.orthogonal_scan_orders(slots, adjacency)
            )
        )

        target, swaps, planned_adjacency = main.optimize_orthogonal_plan(slots)

        self.assertEqual([], swaps)
        self.assertEqual(labels, target)
        self.assertTrue(
            main.labels_are_orthogonally_connected(target, planned_adjacency)
        )

    def test_orthogonal_optimizer_prefers_blocks_over_zero_swap_lines(self):
        current = ["a", "a", "a", "a", "b", "b", "b", "b"]
        slots = self.make_grid_slots(current, columns=4)

        target, swaps, adjacency = main.optimize_orthogonal_plan(slots)

        self.assertEqual(0, main.layout_compactness_score(slots, target, adjacency)[0])
        self.assertGreater(len(swaps), 0)
        self.assertTrue(main.labels_are_orthogonally_connected(target, adjacency))

    def test_largest_orthogonal_component_excludes_distant_screen_match(self):
        slots = self.make_grid_slots(["a", "a", "b", "b"], columns=2)
        noise = types.SimpleNamespace(
            label="a",
            center=(500, 500),
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

        swaps = main.plan_swaps([], current, target, dist)

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

        swaps = main.plan_swaps([], current, target, dist)

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

            swaps = main.plan_swaps([], current, target, dist)

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
            main.plan_swaps([], ["a", "b"], ["a", "a"], np.zeros((2, 2)))

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
