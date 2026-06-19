import sys
import types
import unittest
from unittest import mock

fake_pyautogui = types.ModuleType("pyautogui")
fake_pyautogui.FAILSAFE = True
fake_pyautogui.PAUSE = 0.0
sys.modules.setdefault("pyautogui", fake_pyautogui)

import main  # noqa: E402


def make_slots(labels, points):
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


class OptimizerRepeatTests(unittest.TestCase):
    def test_candidate_orders_ignore_current_slot_order(self):
        self.assertEqual(
            list(main.candidate_label_orders(["b", "a", "c", "a"])),
            list(main.candidate_label_orders(["a", "c", "a", "b"])),
        )

    def test_line_scores_worse_than_l_shape_with_equal_contacts(self):
        labels = ["a", "a", "a"]
        line_slots = make_slots(labels, [(0, 0), (40, 20), (80, 40)])
        l_slots = make_slots(labels, [(0, 0), (40, 20), (0, 40)])

        line_score = main.layout_compactness_score(
            line_slots,
            labels,
            main.build_isometric_adjacency(line_slots),
        )
        l_score = main.layout_compactness_score(
            l_slots,
            labels,
            main.build_isometric_adjacency(l_slots),
        )

        self.assertEqual((1, -2), line_score)
        self.assertEqual((0, -2), l_score)

    def test_optimizer_converges_then_repeats_with_zero_swaps(self):
        current = ["a", "a", "a", "b", "b", "b"]
        compact = ["a", "a", "b", "a", "b", "b"]
        points = [(0, 0), (40, 20), (80, 40), (-40, 20), (0, 40), (40, 60)]

        def optimize(labels):
            slots = make_slots(labels, points)

            with (
                mock.patch.object(
                    main,
                    "orthogonal_scan_orders",
                    return_value=[tuple(range(len(slots)))],
                ),
                mock.patch.object(main, "candidate_label_orders", return_value=[]),
                mock.patch.object(
                    main,
                    "candidate_targets_for_scan",
                    return_value=[compact],
                ),
            ):
                return main.optimize_isometric_plan(slots)

        target, swaps, adjacency = optimize(current)

        self.assertEqual(compact, target)
        self.assertGreater(len(swaps), 0)
        self.assertTrue(main.labels_are_cardinally_connected(target, adjacency))

        repeated_target, repeated_swaps, _ = optimize(target)

        self.assertEqual(target, repeated_target)
        self.assertEqual([], repeated_swaps)


if __name__ == "__main__":
    unittest.main()
