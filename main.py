import time
import pyautogui

from src.config import Config
from src.detection import capture_game_bgr, detect_all_items, save_detection_debug_images, save_merge_debug_image, focus_game_window
from src.geometry import stable_sort_slots, build_isometric_adjacency
from src.planner import optimize_isometric_plan, plan_merge_triggers
from src.executor import execute_swaps, execute_merges


def detect_slots(config, save_debug=True, suffix=""):
    """Captures the board and returns (screenshot_img, offset, slots)."""
    screenshot_img, offset = capture_game_bgr(config)
    diagnostics = {}
    detections = detect_all_items(screenshot_img, config=config, diagnostics=diagnostics, offset=offset)
    slots = stable_sort_slots(detections)
    if save_debug:
        save_detection_debug_images(
            screenshot_img,
            slots,
            config=config,
            diagnostics=diagnostics,
            image_offset=offset,
            suffix=suffix,
        )
    print(f"Detected {len(detections)} items.")
    return screenshot_img, offset, slots


def main():
    config = Config()
    # Archive debug logs in unique run subdirectories to enable future analysis
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    config.detection_debug_dir = config.detection_debug_dir / f"run_{timestamp}"

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = config.swap_settle_delay

    # ── Phase 1: detect → plan align swaps → execute ─────────────────────────
    screenshot_img, offset, slots = detect_slots(config)

    if not slots:
        print("No items detected. Check the game window or lower THRESHOLD.")
        return

    print("Planning...")
    started = time.perf_counter()
    _, phase1_swaps, _ = optimize_isometric_plan(slots, config)
    print(f"Done in {time.perf_counter() - started:.2f}s: {len(phase1_swaps)} align swaps.")

    focus_game_window(config)

    if phase1_swaps:
        print(f"\nPhase 1: aligning ({len(phase1_swaps)} swaps)...")
        execute_swaps(slots, phase1_swaps, config)

    # ── Phase 2: fresh detect → plan merge triggers → execute ─────────────────
    screenshot_img, offset, slots = detect_slots(config, save_debug=True, suffix="_after_phase1")

    if not slots:
        print("Phase 2: no items detected after Phase 1.")
        return

    adjacency = build_isometric_adjacency(slots, config)
    current_labels = [slot.label for slot in slots]
    merge_triggers = plan_merge_triggers(current_labels, adjacency, config.max_group_size)
    merge_debug_path = save_merge_debug_image(
        screenshot_img, slots, merge_triggers, config, image_offset=offset
    )
    print(f"Merge debug: {merge_debug_path} ({len(merge_triggers)} triggers)")
    if merge_triggers:
        print(f"\nPhase 2: merging ({len(merge_triggers)} triggers)...")
        execute_merges(slots, merge_triggers, config)


if __name__ == "__main__":
    main()
