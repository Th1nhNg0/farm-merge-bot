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

import main
from src.config import Config
import src.detection as detection
import src.geometry as geometry
import src.planner as planner
import src.executor as executor


def apply_swaps(labels, swaps):
    result = labels[:]

    for swap in swaps:
        source = swap["from_slot"]
        destination = swap["to_slot"]
        result[source], result[destination] = result[destination], result[source]

    return result


class MockScreenShot:
    def __init__(self, data):
        self.data = data

    def __array__(self, *args, **kwargs):
        return self.data


class SwapPlanningTests(unittest.TestCase):
    def make_isometric_slots(self, labels, points):
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

    def connected_group(self, start, allowed, adjacency):
        visited = set()
        pending = [start]

        while pending:
            index = pending.pop()

            if index in visited:
                continue

            visited.add(index)
            pending.extend((adjacency[index] & allowed) - visited)

        return visited

    def test_drag_moves_quickly_to_source_but_keeps_drag_timing(self):
        events = []
        config = Config()

        with (
            mock.patch.object(
                executor.pyautogui,
                "moveTo",
                side_effect=lambda x, y, duration, **kwargs: events.append(
                    ("move", x, y, duration, kwargs)
                ),
                create=True,
            ),
            mock.patch.object(
                executor.pyautogui,
                "mouseDown",
                side_effect=lambda: events.append(("down",)),
                create=True,
            ),
            mock.patch.object(
                executor.pyautogui,
                "mouseUp",
                side_effect=lambda: events.append(("up",)),
                create=True,
            ),
            mock.patch.object(executor.time, "sleep") as sleep,
        ):
            executor.drag_swap((10, 20), (30, 40), config)

        self.assertEqual(
            [
                ("move", 10, 20, 0, {"_pause": False}),
                ("down",),
                ("move", 30, 40, config.drag_duration, {}),
                ("up",),
            ],
            events,
        )
        sleep.assert_called_once_with(config.swap_settle_delay)

    def test_isometric_adjacency_excludes_logical_diagonal(self):
        config = Config()
        slots = self.make_isometric_slots(
            ["a", "b", "c", "d"],
            [(0, 0), (40, 20), (-40, 20), (0, 40)],
        )

        adjacency = geometry.build_isometric_adjacency(slots, config)

        self.assertEqual({1, 2}, adjacency[0])
        self.assertNotIn(3, adjacency[0])


    def test_template_paths_include_only_base_and_underscore_variants(self):
        config = Config()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config.template_dir = directory

            for name in ("bo1.png", "bo1_2.png", "bo10.png", "bo2.png"):
                (directory / name).touch()

            paths = detection.template_paths("bo", 1, config)

        self.assertEqual(["bo1.png", "bo1_2.png"], [path.name for path in paths])

    def test_template_paths_allow_variant_only_template_sets(self):
        config = Config()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config.template_dir = directory

            for name in ("bo1_1.png", "bo1_2.png", "bo2_1.png"):
                (directory / name).touch()

            paths = detection.template_paths("bo", 1, config)

        self.assertEqual(["bo1_1.png", "bo1_2.png"], [path.name for path in paths])

    def test_center_deduplication_keeps_adjacent_overlapping_items(self):
        config = Config()
        detections = [
            detection.Detection("a_1", 0, 0, 40, 40, 0.90),
            detection.Detection("b_1", 2, 1, 40, 40, 0.95),
            detection.Detection("c_1", 25, 0, 40, 40, 0.85),
        ]

        kept = detection.deduplicate_detections(detections, config)

        self.assertEqual(["b_1", "c_1"], [d.label for d in kept])

    def test_multiscale_detection_finds_a_scaled_template(self):
        config = Config()
        rng = np.random.default_rng(7)
        template = rng.integers(0, 256, size=(20, 24, 3), dtype=np.uint8)
        scaled = cv2.resize(template, (26, 22), interpolation=cv2.INTER_CUBIC)
        screenshot = np.zeros((80, 100, 3), dtype=np.uint8)
        screenshot[30:52, 40:66] = scaled

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            cv2.imwrite(str(directory / "test1.png"), template)

            config.template_dir = directory
            config.template_scales = (1.0, 1.1)
            config.threshold = 0.75
            config.items = ["test"]
            config.levels = [1]
            config.item_levels = {}

            diagnostics = {}
            detections = detection.detect_all_items(
                screenshot,
                config=config,
                diagnostics=diagnostics,
            )

        best = max(detections, key=lambda d: d.score)
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
        d = detection.Detection("bo_1", 10, 20, 30, 40, 0.9)

        self.assertEqual((25, 40), d.center)

    def test_find_game_region_ignores_surrounding_desktop_panels(self):
        config = Config()
        config.game_crop_padding = 0
        screenshot = np.zeros((500, 900, 3), dtype=np.uint8)
        screenshot[70:430, 220:710] = (40, 180, 80)
        screenshot[100:400, 20:160] = (50, 50, 50)

        region = detection.find_game_region(screenshot, config)

        self.assertEqual((220, 70, 490, 360), region)

    def test_find_game_region_rejects_desktop_without_game(self):
        config = Config()
        screenshot = np.full((500, 900, 3), 25, dtype=np.uint8)

        self.assertIsNone(detection.find_game_region(screenshot, config))

    def test_cropped_detection_keeps_absolute_screen_coordinates(self):
        config = Config()
        rng = np.random.default_rng(11)
        template = rng.integers(0, 256, size=(18, 22, 3), dtype=np.uint8)
        crop = np.zeros((80, 100, 3), dtype=np.uint8)
        crop[30:48, 40:62] = template

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            cv2.imwrite(str(directory / "test1.png"), template)

            config.template_dir = directory
            config.threshold = 0.70
            config.items = ["test"]
            config.levels = [1]
            config.item_levels = {}

            detections = detection.detect_all_items(crop, config=config, offset=(200, 70))

        best = max(detections, key=lambda d: d.score)
        self.assertEqual((251, 109), best.center)

    def test_main_always_writes_debug_output(self):
        screenshot = np.zeros((80, 100, 3), dtype=np.uint8)

        with tempfile.TemporaryDirectory() as temp_dir:
            debug_dir = Path(temp_dir)
            config = Config()
            config.detection_debug_dir = debug_dir

            with (
                mock.patch.object(
                    main,
                    "capture_game_bgr",
                    return_value=(screenshot, (0, 0)),
                ),
                mock.patch.object(main, "detect_all_items", return_value=[]),
                mock.patch("main.Config", return_value=config),
                mock.patch("builtins.print"),
            ):
                main.main()

            # Find the run directory created inside debug_dir
            run_dirs = list(debug_dir.glob("run_*"))
            self.assertEqual(1, len(run_dirs))
            actual_debug_dir = run_dirs[0]
            self.assertTrue((actual_debug_dir / "board.png").is_file())
            self.assertTrue((actual_debug_dir / "detections.png").is_file())
            self.assertTrue((actual_debug_dir / "scores.csv").is_file())

    def test_detect_slots_saves_with_custom_suffix(self):
        screenshot = np.zeros((80, 100, 3), dtype=np.uint8)
        config = Config()

        with tempfile.TemporaryDirectory() as temp_dir:
            debug_dir = Path(temp_dir)
            config.detection_debug_dir = debug_dir

            with (
                mock.patch.object(main, "capture_game_bgr", return_value=(screenshot, (0, 0))),
                mock.patch.object(main, "detect_all_items", return_value=[]),
            ):
                main.detect_slots(config, save_debug=True, suffix="_after_phase1")

            self.assertTrue((debug_dir / "board_after_phase1.png").is_file())
            self.assertTrue((debug_dir / "detections_after_phase1.png").is_file())
            self.assertTrue((debug_dir / "scores_after_phase1.csv").is_file())

    def test_optimizer_never_swaps_identical_base_labels(self):
        config = Config()
        current = ["bo_1", "ga_1", "bo_1", "ga_1"]
        points = [(0, 0), (40, 20), (80, 40), (120, 60)]
        slots = self.make_isometric_slots(current, points)

        target, swaps, _ = planner.optimize_isometric_plan(slots, config)

        for swap in swaps:
            self.assertNotEqual(swap["moving_label"], swap["replaced_label"])

    def test_detect_slots_keeps_all_cropped_detections(self):
        screenshot = np.zeros((80, 100, 3), dtype=np.uint8)
        config = Config()
        slots = self.make_isometric_slots(
            ["a", "a", "b", "b"],
            [(0, 0), (40, 20), (-40, 20), (0, 40)],
        )
        noise = types.SimpleNamespace(
            label="a",
            center=(500, 500),
            grid_anchor=(500, 500),
            w=20,
            h=20,
        )

        with (
            mock.patch.object(main, "capture_game_bgr", return_value=(screenshot, (0, 0))),
            mock.patch.object(main, "detect_all_items", return_value=slots + [noise]),
            mock.patch("builtins.print"),
        ):
            _, _, detected_slots = main.detect_slots(config, save_debug=False)

        self.assertEqual(5, len(detected_slots))
        self.assertIn(noise, detected_slots)

    def test_detection_debug_marks_excluded_slots(self):
        screenshot = np.zeros((40, 60, 3), dtype=np.uint8)

        with tempfile.TemporaryDirectory() as temp_dir:
            debug_dir = Path(temp_dir)
            config = Config()
            config.detection_debug_dir = debug_dir
            kept = detection.Detection("a_1", 5, 5, 10, 10, 0.9)
            excluded = detection.Detection("b_1", 30, 5, 10, 10, 0.8)

            detection.save_detection_debug_images(
                screenshot,
                [kept],
                config=config,
                excluded_detections=[excluded],
            )

            annotated = cv2.imread(str(debug_dir / "detections.png"))

        self.assertEqual([0, 255, 0], annotated[5, 5].tolist())
        self.assertEqual([0, 165, 255], annotated[5, 30].tolist())

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

        swaps = planner.plan_swaps(current, target, dist)

        self.assertEqual(2, len(swaps))
        self.assertEqual(target, apply_swaps(current, swaps))
        self.assertEqual(
            [(1, 0), (2, 1)],
            [(swap["from_slot"], swap["to_slot"]) for swap in swaps],
        )

    def test_plan_swaps_accepts_tuple_targets(self):
        current = ["a", "b", "c"]
        target = ("b", "c", "a")
        dist = np.ones((3, 3))

        swaps = planner.plan_swaps(current, target, dist)

        self.assertEqual(list(target), apply_swaps(current, swaps))

    def test_bounded_swap_planning_aborts_after_limit(self):
        current = ["a", "b", "c"]
        target = ["b", "c", "a"]
        dist = np.ones((3, 3))

        self.assertIsNone(planner._plan_swaps(current, target, dist, max_swaps=1))

    def test_merge_triggers_use_five_slots_from_oversized_components(self):
        labels = ["a"] * 6
        adjacency = {
            index: {
                neighbor
                for neighbor in (index - 1, index + 1)
                if 0 <= neighbor < len(labels)
            }
            for index in range(len(labels))
        }

        triggers = planner.plan_merge_triggers(labels, adjacency, 5)

        self.assertEqual(1, len(triggers))
        self.assertEqual("a", triggers[0]["label"])
        self.assertEqual(
            {2, 3, 4, 5},
            self.connected_group(
                triggers[0]["to_slot"],
                set(range(len(labels))) - {triggers[0]["from_slot"]},
                adjacency,
            ),
        )

    def test_merge_triggers_skip_when_no_exact_four_item_target_group(self):
        labels = ["a"] * 6
        adjacency = {
            index: set(range(len(labels))) - {index}
            for index in range(len(labels))
        }

        with mock.patch("builtins.print"):
            self.assertEqual([], planner.plan_merge_triggers(labels, adjacency, 5))

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

        swaps = planner.plan_swaps(current, target, dist)

        self.assertEqual(target, apply_swaps(current, swaps))
        self.assertEqual(
            {frozenset((0, 2)), frozenset((1, 3))},
            {
                frozenset((swap["from_slot"], swap["to_slot"]))
                for swap in swaps
            },
        )

    def test_shortest_cycles_returns_all_reciprocal_cycles(self):
        edge_slots = {
            ("a", "b"): [0],
            ("b", "a"): [1],
            ("b", "c"): [2],
            ("c", "b"): [3],
            ("c", "a"): [4],
        }

        self.assertEqual(
            [
                (("a", "b"), ("b", "a")),
                (("b", "c"), ("c", "b")),
            ],
            planner._shortest_label_cycles(edge_slots),
        )

    def test_optimizer_stops_planning_after_zero_swap_candidate(self):
        config = Config()
        labels = list("abcdef")
        slots = self.make_isometric_slots(
            labels,
            [(0, 0), (40, 20), (80, 40), (120, 60), (160, 80), (200, 100)],
        )
        adjacency = {
            index: set(range(len(slots))) - {index} for index in range(len(slots))
        }

        with (
            mock.patch.object(geometry, "build_isometric_adjacency", return_value=adjacency),
            mock.patch.object(
                planner,
                "orthogonal_scan_orders",
                return_value=[tuple(range(len(slots)))],
            ),
            mock.patch.object(planner, "_plan_swaps", wraps=planner._plan_swaps) as mock_planner,
        ):
            target, swaps, planned_adjacency = planner.optimize_isometric_plan(slots, config)

        self.assertEqual(0, mock_planner.call_count)
        self.assertEqual(target, apply_swaps(labels, swaps))
        self.assertTrue(
            planner.labels_are_cardinally_connected(target, planned_adjacency)
        )

        with (
            mock.patch.object(geometry, "build_isometric_adjacency", return_value=adjacency),
            mock.patch.object(
                planner,
                "orthogonal_scan_orders",
                return_value=[tuple(range(len(slots)))],
            ),
        ):
            repeated = planner.optimize_isometric_plan(slots, config)

        self.assertEqual((target, swaps), repeated[:2])

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

            swaps = planner.plan_swaps(current, target, dist)

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
            planner.plan_swaps(["a", "b"], ["a", "a"], np.zeros((2, 2)))

    def test_execute_swaps_has_no_limit(self):
        config = Config()
        slots = [
            types.SimpleNamespace(
                label="a" if i % 2 == 0 else "b",
                center=(i, i),
                grid_anchor=(i, i),
            )
            for i in range(102)
        ]
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
            mock.patch.object(executor, "drag_swap") as drag_swap,
            mock.patch.object(executor.time, "sleep"),
            mock.patch("builtins.print"),
        ):
            executor.execute_swaps(slots, swaps, config)

        self.assertEqual(101, drag_swap.call_count)

    def test_execute_swaps_tracks_sprite_center_after_it_moves(self):
        config = Config()
        slots = [
            types.SimpleNamespace(
                label="a", center=(100, 80), grid_anchor=(100, 100)
            ),
            types.SimpleNamespace(
                label="b", center=(200, 70), grid_anchor=(200, 100)
            ),
            types.SimpleNamespace(
                label="c", center=(300, 90), grid_anchor=(300, 100)
            ),
        ]
        swaps = [
            {
                "from_slot": 1,
                "to_slot": 0,
                "moving_label": "b",
                "replaced_label": "a",
            },
            {
                "from_slot": 0,
                "to_slot": 2,
                "moving_label": "b",
                "replaced_label": "c",
            },
        ]

        with (
            mock.patch.object(executor, "drag_swap") as drag_swap,
            mock.patch.object(executor.time, "sleep"),
            mock.patch("builtins.print"),
        ):
            executor.execute_swaps(slots, swaps, config)

        self.assertEqual(
            [
                mock.call((200, 70), (100, 80), config),
                mock.call((100, 70), (300, 90), config),
            ],
            drag_swap.call_args_list,
        )

    def test_capture_game_bgr_falls_back_to_primary_monitor(self):
        config = Config()
        config.window_title = None

        mock_region = (10, 10, 50, 50)

        # Mock mss
        mock_mss_instance = mock.MagicMock()
        mock_mss_instance.__enter__.return_value = mock_mss_instance
        mock_mss_instance.monitors = [None, {"left": 0, "top": 0, "width": 800, "height": 600}]
        mock_grab_result = MockScreenShot(np.zeros((600, 800, 4), dtype=np.uint8))
        mock_mss_instance.grab.return_value = mock_grab_result

        with (
            mock.patch("src.detection.mss.mss", return_value=mock_mss_instance),
            mock.patch("src.detection.find_game_region", return_value=mock_region),
        ):
            img, offset = detection.capture_game_bgr(config)

        self.assertEqual((10, 10), offset)
        self.assertEqual((50, 50, 3), img.shape)

    def test_capture_game_bgr_with_window_title_finds_window(self):
        config = Config()
        config.window_title = "My Game Window"

        # Mock window
        mock_window = mock.MagicMock()
        mock_window.left = 100
        mock_window.top = 200
        mock_window.width = 400
        mock_window.height = 300
        mock_window.isMinimized = False

        mock_windows = [mock_window]

        # Mock mss
        mock_mss_instance = mock.MagicMock()
        mock_mss_instance.__enter__.return_value = mock_mss_instance
        mock_grab_result = MockScreenShot(np.zeros((300, 400, 4), dtype=np.uint8))
        mock_mss_instance.grab.return_value = mock_grab_result

        with (
            mock.patch("src.detection.gw.getWindowsWithTitle", return_value=mock_windows),
            mock.patch("src.detection.mss.mss", return_value=mock_mss_instance),
            mock.patch("src.detection.find_game_region", return_value=None), # fallback to whole window
        ):
            img, offset = detection.capture_game_bgr(config)

        self.assertEqual((100, 200), offset)
        self.assertEqual((300, 400, 3), img.shape)
        mock_mss_instance.grab.assert_called_once_with({
            "top": 200,
            "left": 100,
            "width": 400,
            "height": 300,
        })


if __name__ == "__main__":
    unittest.main()
