import time
import pyautogui

from src.config import Config
from src.detection import capture_game_bgr, detect_all_items, save_detection_debug_images
from src.geometry import stable_sort_slots, largest_orthogonal_component
from src.planner import optimize_isometric_plan
from src.executor import execute_swaps


def print_adjacency_summary(adjacency, target_labels):
    """Prints a concise verification that no diagonal-only groups remain."""
    edge_count = sum(len(neighbors) for neighbors in adjacency.values()) // 2
    print(f"Isometric cardinal adjacency edges: {edge_count}")

    for label in sorted(set(target_labels)):
        indices = [i for i, value in enumerate(target_labels) if value == label]
        if len(indices) <= 1:
            continue

        internal_contacts = (
            sum(len(adjacency[index] & set(indices)) for index in indices) // 2
        )
        print(
            f"Group {label}: {len(indices)} slots, "
            f"{internal_contacts} cardinal contacts"
        )


def main():
    config = Config()

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = config.swap_settle_delay  # Default pause matching settle delay

    screenshot_img, offset = capture_game_bgr(config)
    print(
        f"Game region: x={offset[0]}, y={offset[1]}, "
        f"w={screenshot_img.shape[1]}, h={screenshot_img.shape[0]}"
    )

    diagnostics = {}
    detections = detect_all_items(
        screenshot_img,
        config=config,
        diagnostics=diagnostics,
        offset=offset,
    )
    raw_path, annotated_path, scores_path = save_detection_debug_images(
        screenshot_img,
        detections,
        config=config,
        diagnostics=diagnostics,
        image_offset=offset,
    )
    print(f"Detection debug files: {raw_path}, {annotated_path}, {scores_path}")

    print(f"Detected {len(detections)} items.")

    if not detections:
        print("No items detected. Check the game window or lower THRESHOLD.")
        return

    all_slots = stable_sort_slots(detections)
    slots = largest_orthogonal_component(all_slots, config)
    excluded_count = len(all_slots) - len(slots)

    if excluded_count:
        print(
            f"Excluded {excluded_count} detections outside the main "
            "isometric item grid."
        )

    print("Planning swaps...")
    started = time.perf_counter()
    target_labels, swaps, adjacency = optimize_isometric_plan(slots, config)
    planning_seconds = time.perf_counter() - started

    print(f"Planned {len(swaps)} swaps in {planning_seconds:.2f}s.")
    print(f"Target label order: {target_labels}")
    print_adjacency_summary(adjacency, target_labels)

    print("\nSwap plan:")
    for i, swap in enumerate(swaps, start=1):
        print(
            f"{i}. slot {swap['from_slot']} -> slot {swap['to_slot']} | "
            f"{swap['moving_label']} swaps with {swap['replaced_label']}"
        )

    execute_swaps(slots, swaps, config)


if __name__ == "__main__":
    main()
