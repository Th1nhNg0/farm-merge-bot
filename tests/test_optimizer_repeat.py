import itertools
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

    def test_optimizer_preserves_best_compactness_before_reducing_swaps(self):
        current = ["a", "b", "b", "a", "a", "b", "b", "a", "b"]
        denser = ["a", "a", "a", "a", "b", "b", "b", "b", "b"]
        points = [
            (0, 0),
            (40, 20),
            (80, 40),
            (-40, 20),
            (0, 40),
            (40, 60),
            (80, 80),
            (-40, 60),
            (0, 80),
        ]
        slots = make_slots(current, points)

        adjacency = main.build_isometric_adjacency(slots)
        self.assertTrue(main.labels_are_cardinally_connected(current, adjacency))
        self.assertTrue(main.labels_are_cardinally_connected(denser, adjacency))
        self.assertLess(
            main.layout_compactness_score(slots, denser, adjacency),
            main.layout_compactness_score(slots, current, adjacency),
        )

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
                return_value=[denser],
            ),
        ):
            target, swaps, _ = main.optimize_isometric_plan(slots)

        self.assertEqual(
            main.layout_compactness_score(slots, denser, adjacency),
            main.layout_compactness_score(slots, target, adjacency),
        )
        self.assertTrue(main.labels_are_cardinally_connected(target, adjacency))
        self.assertGreater(len(swaps), 0)

    def test_target_repair_reduces_swaps_without_changing_quality(self):
        current = ["a", "a", "b", "a", "a", "b", "b", "b", "b"]
        target = ["a", "a", "a", "a", "b", "b", "b", "b", "b"]
        points = [
            (0, 0),
            (40, 20),
            (80, 40),
            (-40, 20),
            (0, 40),
            (40, 60),
            (80, 80),
            (-40, 60),
            (0, 80),
        ]
        slots = make_slots(current, points)
        adjacency = main.build_isometric_adjacency(slots)
        dist = main.pairwise_distance_matrix(slots)
        swaps = main.plan_swaps(current, target, dist)
        quality = main.layout_compactness_score(slots, target, adjacency)

        repaired_target, repaired_swaps = main.refine_target_assignments(
            slots,
            current,
            target,
            adjacency,
            dist,
            swaps,
        )

        self.assertEqual(current, repaired_target)
        self.assertEqual([], repaired_swaps)
        self.assertEqual(
            quality,
            main.layout_compactness_score(slots, repaired_target, adjacency),
        )
        self.assertTrue(
            main.labels_are_cardinally_connected(repaired_target, adjacency)
        )

    def test_optimizer_checks_beyond_shortlist_when_fewer_swaps_are_possible(self):
        labels = list("abcdef")
        points = [(index * 40, index * 20) for index in range(len(labels))]
        slots = make_slots(labels, points)
        adjacency = {
            index: set(range(len(labels))) - {index}
            for index in range(len(labels))
        }
        orders = list(itertools.islice(itertools.permutations(labels), 40))
        targets = [
            main.target_labels_for_scan(labels, tuple(range(len(labels))), order)
            for order in orders
            if list(order) != labels
        ]
        ordered_targets = sorted(
            targets,
            key=lambda target: (
                sum(current != wanted for current, wanted in zip(labels, target)),
                tuple(target),
            ),
        )
        better_target = ordered_targets[main.PLAN_SHORTLIST_SIZE]

        def compactness(_slots, target, _adjacency):
            return (1, 0) if target == labels else (0, 0)

        def plan(_current, target, _dist):
            count = 2 if target == better_target else 3
            return [
                {
                    "from_slot": 1,
                    "to_slot": 0,
                    "moving_label": "b",
                    "replaced_label": "a",
                }
                for _ in range(count)
            ]

        with (
            mock.patch.object(main, "build_isometric_adjacency", return_value=adjacency),
            mock.patch.object(
                main,
                "orthogonal_scan_orders",
                return_value=[tuple(range(len(labels)))],
            ),
            mock.patch.object(main, "candidate_label_orders", return_value=orders),
            mock.patch.object(main, "candidate_targets_for_scan", return_value=[]),
            mock.patch.object(main, "layout_compactness_score", side_effect=compactness),
            mock.patch.object(main, "plan_swaps", side_effect=plan) as planner,
            mock.patch.object(
                main,
                "refine_target_assignments",
                side_effect=lambda slots, current, target, adjacency, dist, swaps: (
                    target,
                    swaps,
                ),
            ),
        ):
            target, swaps, _ = main.optimize_isometric_plan(slots)

        self.assertGreater(planner.call_count, main.PLAN_SHORTLIST_SIZE)
        self.assertEqual(better_target, target)
        self.assertEqual(2, len(swaps))


if __name__ == "__main__":
    unittest.main()
