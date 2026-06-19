import random
import sys
import types
import unittest
from unittest import mock

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
    def test_detection_screen_click_position_is_bottom_center(self):
        detection = main.Detection("bo_1", "bo", 1, 10, 20, 30, 40, 0.9)

        self.assertEqual(
            (main.SCREEN_X0 + 25, main.SCREEN_Y0 + 60),
            detection.screen_center,
        )

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
