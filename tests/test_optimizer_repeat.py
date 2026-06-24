import itertools
import random
import sys
import types
import unittest
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
    def test_candidate_orders_ignore_current_slot_order(self):
        config = Config()
        self.assertEqual(
            list(planner.candidate_label_orders(["b", "a", "c", "a"], config)),
            list(planner.candidate_label_orders(["a", "c", "a", "b"], config)),
        )

    def test_line_scores_worse_than_l_shape_with_equal_contacts(self):
        config = Config()
        labels = ["a", "a", "a"]
        line_slots = make_slots(labels, [(0, 0), (40, 20), (80, 40)])
        l_slots = make_slots(labels, [(0, 0), (40, 20), (0, 40)])

        line_score = planner.layout_compactness_score(
            line_slots,
            labels,
            geometry.build_isometric_adjacency(line_slots, config),
            config,
        )
        l_score = planner.layout_compactness_score(
            l_slots,
            labels,
            geometry.build_isometric_adjacency(l_slots, config),
            config,
        )

        self.assertEqual((0, 1, 0, -2), line_score)
        self.assertEqual((0, 0, 0, -2), l_score)

    def test_levels_of_the_same_item_prefer_adjacent_groups(self):
        config = Config()
        points = [(0, 0), (40, 20), (-40, 20), (0, 40)]
        separated = ["bo_1", "ga_1", "ga_2", "bo_2"]
        adjacent = ["bo_1", "bo_2", "ga_1", "ga_2"]
        slots = make_slots(separated, points)
        adjacency = geometry.build_isometric_adjacency(slots, config)

        self.assertLess(
            planner.layout_compactness_score(slots, adjacent, adjacency, config),
            planner.layout_compactness_score(slots, separated, adjacency, config),
        )

        with (
            mock.patch.object(
                planner,
                "orthogonal_scan_orders",
                return_value=[tuple(range(len(slots)))],
            ),
            mock.patch.object(planner, "candidate_label_orders", return_value=[]),
            mock.patch.object(
                planner,
                "candidate_targets_for_scan",
                return_value=[adjacent],
            ),
        ):
            target, swaps, _ = planner.optimize_isometric_plan(slots, config)

        self.assertEqual(
            planner.layout_compactness_score(slots, adjacent, adjacency, config),
            planner.layout_compactness_score(slots, target, adjacency, config),
        )
        bo_slots = {index for index, label in enumerate(target) if label.startswith("bo_")}
        self.assertTrue(any(adjacency[index] & bo_slots for index in bo_slots))
        self.assertGreater(len(swaps), 0)

    def test_scan_candidates_ignore_current_slot_order(self):
        config = Config()
        first = ["bo_1", "bo_1", "bo_2", "ga_1", "ga_1", "ga_2"]
        second = ["ga_1", "bo_2", "bo_1", "ga_2", "bo_1", "ga_1"]
        scan_order = tuple(range(len(first)))
        adjacency = {
            index: set(range(len(first))) - {index}
            for index in range(len(first))
        }

        first_candidates = list(
            planner.candidate_targets_for_scan(
                first,
                scan_order,
                adjacency,
                random.Random(config.label_order_seed),
            )
        )
        second_candidates = list(
            planner.candidate_targets_for_scan(
                second,
                scan_order,
                adjacency,
                random.Random(config.label_order_seed),
            )
        )

        self.assertEqual(first_candidates, second_candidates)

    def test_optimizer_converges_then_repeats_with_zero_swaps(self):
        config = Config()
        current = ["a", "a", "a", "b", "b", "b"]
        compact = ["a", "a", "b", "a", "b", "b"]
        points = [(0, 0), (40, 20), (80, 40), (-40, 20), (0, 40), (40, 60)]

        def optimize(labels):
            slots = make_slots(labels, points)

            with (
                mock.patch.object(
                    planner,
                    "orthogonal_scan_orders",
                    return_value=[tuple(range(len(slots)))],
                ),
                mock.patch.object(planner, "candidate_label_orders", return_value=[]),
                mock.patch.object(
                    planner,
                    "candidate_targets_for_scan",
                    return_value=[compact],
                ),
            ):
                return planner.optimize_isometric_plan(slots, config)

        target, swaps, adjacency = optimize(current)

        self.assertEqual(compact, target)
        self.assertGreater(len(swaps), 0)
        self.assertTrue(planner.labels_are_cardinally_connected(target, adjacency))

        repeated_target, repeated_swaps, _ = optimize(target)

        self.assertEqual(target, repeated_target)
        self.assertEqual([], repeated_swaps)

    def test_optimizer_preserves_best_compactness_before_reducing_swaps(self):
        config = Config()
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

        adjacency = geometry.build_isometric_adjacency(slots, config)
        self.assertTrue(planner.labels_are_cardinally_connected(current, adjacency))
        self.assertTrue(planner.labels_are_cardinally_connected(denser, adjacency))
        self.assertLess(
            planner.layout_compactness_score(slots, denser, adjacency, config),
            planner.layout_compactness_score(slots, current, adjacency, config),
        )

        with (
            mock.patch.object(
                planner,
                "orthogonal_scan_orders",
                return_value=[tuple(range(len(slots)))],
            ),
            mock.patch.object(planner, "candidate_label_orders", return_value=[]),
            mock.patch.object(
                planner,
                "candidate_targets_for_scan",
                return_value=[denser],
            ),
        ):
            target, swaps, _ = planner.optimize_isometric_plan(slots, config)

        self.assertEqual(
            planner.layout_compactness_score(slots, denser, adjacency, config),
            planner.layout_compactness_score(slots, target, adjacency, config),
        )
        self.assertTrue(planner.labels_are_cardinally_connected(target, adjacency))
        self.assertGreater(len(swaps), 0)

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
        labels = list("abcdef")
        points = [(index * 40, index * 20) for index in range(len(labels))]
        slots = make_slots(labels, points)
        adjacency = {
            index: set(range(len(labels))) - {index}
            for index in range(len(labels))
        }
        orders = list(itertools.islice(itertools.permutations(labels), 40))
        targets = [
            planner.target_labels_for_scan(labels, tuple(range(len(labels))), order)
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
        cheap_target = ordered_targets[0]

        def scorer(slots, adjacency, config):
            return lambda target: (1, 0) if target == labels else (0, 0)

        def plan(_current, target, _dist, _max_swaps=None):
            return [
                {
                    "from_slot": 1,
                    "to_slot": 0,
                    "moving_label": "b",
                    "replaced_label": "a",
                }
                for _ in range(3)
            ]

        with (
            mock.patch.object(geometry, "build_isometric_adjacency", return_value=adjacency),
            mock.patch.object(
                planner,
                "orthogonal_scan_orders",
                return_value=[tuple(range(len(labels)))],
            ),
            mock.patch.object(planner, "candidate_label_orders", return_value=orders),
            mock.patch.object(planner, "candidate_targets_for_scan", return_value=[]),
            mock.patch.object(planner, "_layout_compactness_scorer", side_effect=scorer),
            mock.patch.object(planner, "_plan_swaps", side_effect=plan) as mock_planner,
        ):
            target, swaps, _ = planner.optimize_isometric_plan(slots, config)

        self.assertEqual(1, mock_planner.call_count)
        self.assertEqual(cheap_target, target)
        self.assertEqual(3, len(swaps))


if __name__ == "__main__":
    unittest.main()
