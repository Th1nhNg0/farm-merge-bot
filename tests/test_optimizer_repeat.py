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
    def test_levels_of_the_same_item_minimize_swaps_for_singles(self):
        config = Config()
        current = ["b", "a", "c", "a"]
        points = [(0, 0), (40, 20), (80, 40), (120, 60)]
        slots = make_slots(current, points)

        target, swaps, _ = planner.optimize_isometric_plan(slots, config)
        
        self.assertEqual(Counter(current), Counter(target))
        self.assertEqual(current, target)
        self.assertEqual([], swaps)

    def test_optimizer_converges_then_repeats_with_zero_swaps(self):
        config = Config()
        current = ["a", "a", "a", "b", "a", "a"]
        points = [(i * 40, i * 20) for i in range(6)]
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
        self.assertEqual({6}, set(bo_runs))
        self.assertEqual([], swaps)


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

    def test_group_assignment_minimizes_swaps(self):
        config = Config()
        current = ["bo_1"] * 3 + ["ga_1"] * 2 + ["bo_1"] * 2 + ["ga_1"] * 3 + ["bo_1"]
        points = [(i * 40, i * 20) for i in range(11)]
        slots = make_slots(current, points)
        
        target, swaps, _ = planner.optimize_isometric_plan(slots, config)
        self.assertGreater(len(swaps), 0)

    def test_custom_item_sort_key_type_based(self):
        # custom_item_sort_key should sort: plants/materials (0), animals (1), coins (2)
        labels = ["xu_1", "bo_1", "carot_1", "ga_1", "mia_1"]
        sorted_labels = sorted(labels, key=planner.custom_item_sort_key)
        expected = ["carot_1", "mia_1", "bo_1", "ga_1", "xu_1"]
        self.assertEqual(expected, sorted_labels)

    def test_custom_item_sort_key_prioritized_on_top(self):
        # go, da, congcu should sort to the top, before standard plants, animals, and coins.
        labels = ["xu_1", "bo_1", "carot_1", "da_1", "ga_1", "go_1", "congcu_1", "mia_1"]
        sorted_labels = sorted(labels, key=planner.custom_item_sort_key)
        expected = ["go_1", "da_1", "congcu_1", "carot_1", "mia_1", "bo_1", "ga_1", "xu_1"]
        self.assertEqual(expected, sorted_labels)

    def test_strict_sort_layout(self):
        config = Config()
        current = ["xu_1", "bo_1", "carot_1"]
        points = [(0, 0), (40, 20), (80, 40)]
        slots = make_slots(current, points)
        
        target, swaps, _ = planner.optimize_isometric_plan(slots, config, strict_sort=True)
        expected = ["carot_1", "bo_1", "xu_1"]
        self.assertEqual(expected, target)
        self.assertGreater(len(swaps), 0)

    def test_strict_sort_ignores_groups(self):
        config = Config()
        current = ["ga_1"] * 5 + ["carot_1"]
        points = [(i * 40, i * 20) for i in range(6)]
        slots = make_slots(current, points)
        
        target, swaps, _ = planner.optimize_isometric_plan(slots, config, strict_sort=True)
        expected = ["carot_1"] + ["ga_1"] * 5
        self.assertEqual(expected, target)

    def test_already_grouped_items_are_not_moved(self):
        config = Config()
        # Create a layout where 5 'bo_1' items are connected, and 5 'ga_1' items are connected.
        current = ["bo_1"] * 5 + ["ga_1"] * 5
        points = [(i * 40, i * 20) for i in range(10)]
        slots = make_slots(current, points)
        
        target, swaps, _ = planner.optimize_isometric_plan(slots, config)
        self.assertEqual(current, target)
        self.assertEqual([], swaps)

    def test_find_box_btn(self):
        import cv2
        import numpy as np
        from main import find_box_btn
        
        config = Config()
        
        # Load the actual box_btn template
        template_path = config.template_dir / "box_btn.png"
        if not template_path.exists():
            self.skipTest("box_btn.png not present in images directory")
            
        template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if template is None:
            self.skipTest("Could not read box_btn.png template")
            
        # Create a blank screenshot (larger than template)
        screenshot_h = template.shape[0] + 100
        screenshot_w = template.shape[1] + 100
        screenshot = np.zeros((screenshot_h, screenshot_w, 3), dtype=np.uint8)
        
        # Paste template at a known location, say (50, 50)
        paste_y, paste_x = 50, 50
        screenshot[paste_y:paste_y+template.shape[0], paste_x:paste_x+template.shape[1]] = template
        
        # Find the box button
        loc = find_box_btn(screenshot, config)
        self.assertIsNotNone(loc)
        
        # Click location should match the center of the pasted template
        expected_x = paste_x + template.shape[1] // 2
        expected_y = paste_y + template.shape[0] // 2
        
        self.assertAlmostEqual(loc[0], expected_x, delta=2)
        self.assertAlmostEqual(loc[1], expected_y, delta=2)


if __name__ == "__main__":
    unittest.main()
