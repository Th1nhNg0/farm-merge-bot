import itertools
import random
import sys
import types
import unittest
from collections import Counter
from unittest import mock

fake_pyautogui = types.ModuleType("pyautogui")
fake_pyautogui.FAILSAFE = True
fake_pyautogui.PAUSE = 0.0
sys.modules.setdefault("pyautogui", fake_pyautogui)

from src.config import Config
import src.geometry as geometry
import src.planner as planner


def make_slots(labels, points):
    return [
        types.SimpleNamespace(
            label=label,
            center=point,
            grid_anchor=point,
            w=20,
            h=20,
        )
        for label, point in zip(labels, points)
    ]


class OptimizerRepeatTests(unittest.TestCase):
    def test_levels_of_the_same_item_prefer_adjacent_groups(self):
        config = Config()
        current = ["b", "a", "c", "a"]
        points = [(0, 0), (40, 20), (80, 40), (120, 60)]
        slots = make_slots(current, points)

        target, swaps, _ = planner.optimize_isometric_plan(slots, config)
        
        self.assertEqual(Counter(current), Counter(target))
        self.assertIn(target, (['a', 'a', 'b', 'c'], ['c', 'b', 'a', 'a']))

    def test_optimizer_converges_then_repeats_with_zero_swaps(self):
        config = Config()
        current = ["b", "a", "c", "a"]
        points = [(0, 0), (40, 20), (80, 40), (120, 60)]
        slots = make_slots(current, points)
        
        target1, swaps1, _ = planner.optimize_isometric_plan(slots, config)
        self.assertGreater(len(swaps1), 0)
        
        slots2 = make_slots(target1, points)
        target2, swaps2, _ = planner.optimize_isometric_plan(slots2, config)
        
        self.assertEqual(target1, target2)
        self.assertEqual([], swaps2)

    def test_optimizer_preserves_best_compactness_before_reducing_swaps(self):
        config = Config()
        current = ["bo_1"] * 6 + ["ga_1"] * 5
        points = [(i * 40, i * 20) for i in range(11)]
        slots = make_slots(current, points)
        
        target, swaps, _ = planner.optimize_isometric_plan(slots, config)
        
        runs = [(k, len(list(g))) for k, g in itertools.groupby(target)]
        bo_runs = [length for item, length in runs if item == "bo_1"]
        self.assertEqual({1, 5}, set(bo_runs))


    def test_split_phase_plans_merge_from_exact_five_group(self):
        config = Config()
        labels = ["a"] * 6
        points = [(index * 40, index * 20) for index in range(len(labels))]
        slots = make_slots(labels, points)
        split_target = ["a§0"] * 5 + ["a§single5"]
        adjacency = {
            index: set(range(len(labels))) - {index}
            for index in range(len(labels))
        }

        with mock.patch.object(
            planner,
            "_optimize_isometric_plan_inner",
            return_value=(split_target, [], adjacency),
        ):
            target, swaps, _ = planner.optimize_isometric_plan(
                slots, config
            )

        self.assertEqual(labels, target)
        self.assertEqual([], swaps)

    def test_optimizer_plans_only_the_best_cheap_candidate(self):
        config = Config()
        current = ["a", "b", "c", "d"]
        points = [(0, 0), (40, 20), (80, 40), (120, 60)]
        slots = make_slots(current, points)
        
        target, swaps, _ = planner.optimize_isometric_plan(slots, config)
        self.assertEqual(current, target)
        self.assertEqual([], swaps)


if __name__ == "__main__":
    unittest.main()
