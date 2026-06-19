import random
import sys
import tempfile
import types
import unittest
from collections import Counter
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
    def test_cluster_optimizer_is_no_worse_than_default_layout(self):
        labels = ["a", "b", "c", "a", "b", "c", "a", "b", "c", "a", "b", "c"]
        slots = [
            types.SimpleNamespace(
                label=label,
                center=((index % 4) * 40.0, (index // 4) * 40.0),
                w=24,
                h=24,
            )
            for index, label in enumerate(labels)
        ]
        dist = main.pairwise_distance_matrix(slots)
        median_nn = main.median_nearest_neighbor_distance(dist)
        default_target, _ = main.make_label_clustered_target_labels(
            slots,
            dist,
            median_nn,
        )
        default_swaps = main.plan_swaps(slots, labels, default_target, dist)

        target, clusters, swaps, _ = main.optimize_clustered_plan(
            slots,
            dist,
            median_nn,
        )

        self.assertLessEqual(len(swaps), len(default_swaps))
        self.assertEqual(Counter(labels), Counter(target))
        self.assertTrue(
            all(
                main.cluster_is_connected(slots, indices, dist, median_nn)
                for indices in clusters.values()
            )
        )

    def test_board_mask_excludes_decorations_outside_merge_field(self):
        mask = main.make_board_mask((600, 850, 3))

        self.assertEqual(0, mask[50, 300])
        self.assertEqual(255, mask[250, 500])
        self.assertEqual(0, mask[500, 100])

    def test_template_paths_include_only_base_and_underscore_variants(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)

            for name in ("bo1.png", "bo1_2.png", "bo10.png", "bo2.png"):
                (directory / name).touch()

            with mock.patch.object(main, "TEMPLATE_DIR", directory):
                paths = main.template_paths("bo", 1)

        self.assertEqual(["bo1.png", "bo1_2.png"], [path.name for path in paths])

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
                mock.patch.object(
                    main,
                    "BOARD_POLYGON",
                    ((0, 0), (99, 0), (99, 79), (0, 79)),
                ),
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

        self.assertEqual(
            (main.SCREEN_X0 + 25, main.SCREEN_Y0 + 40),
            detection.screen_center,
        )

    def test_cluster_selection_minimizes_displaced_items_first(self):
        slots = [
            types.SimpleNamespace(center=(float(x), 0.0), w=1, h=1)
            for x in range(4)
        ]
        points = np.array([slot.center for slot in slots])
        dist = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
        adj = {
            0: {1},
            1: {0, 2},
            2: {1, 3},
            3: {2},
        }

        cluster = main.choose_compact_cluster(
            slots=slots,
            size=2,
            candidate_area=set(range(4)),
            current_label_indices=[0, 3],
            target_point=np.array([1.5, 0.0]),
            adj=adj,
            dist=dist,
            median_nn=0.01,
        )

        self.assertTrue(set(cluster) & {0, 3})

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
