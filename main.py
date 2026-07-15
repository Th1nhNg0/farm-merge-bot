import sys
import time

import cv2
import pyautogui
import pygetwindow as gw

from src.config import Config
from src.detection import (
    capture_game_bgr,
    combined_match_score,
    determine_best_scale,
    detect_all_items,
    focus_game_window,
    matching_features,
    scaled_templates,
)
from src.executor import execute_merges, execute_swaps
from src.geometry import build_isometric_adjacency, stable_sort_slots
from src.planner import optimize_isometric_plan, plan_merge_triggers


def is_game_window_active(config):
    active_title = gw.getActiveWindowTitle()
    if not active_title:
        return False
    if config.window_title:
        return config.window_title.lower() in active_title.lower()
    return "discord" in active_title.lower()


def is_key_pressed(vk_code):
    if sys.platform == "win32":
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)
    return False


def play_sound(kind):
    if sys.platform != "win32":
        sys.stdout.write("\a")
        sys.stdout.flush()
        return

    import winsound

    tones = {
        "start": ((2000, 50),),
        "success": ((2000, 50), (2500, 50)),
        "error": ((600, 250),),
    }
    try:
        for index, (frequency, duration) in enumerate(tones[kind]):
            if index:
                time.sleep(0.05)
            winsound.Beep(frequency, duration)
    except Exception:
        winsound.MessageBeep()


def detect_slots(config):
    screenshot_img, offset = capture_game_bgr(config)
    slots = stable_sort_slots(detect_all_items(screenshot_img, config, offset=offset))
    print(f"Detected {len(slots)} items.")
    return slots


def run_cycle(config):
    slots = detect_slots(config)
    if not slots:
        print("No items detected. Check the game window or lower THRESHOLD.")
        return False

    _, swaps, _ = optimize_isometric_plan(slots, config)
    focus_game_window(config)
    if swaps:
        print(f"Aligning {len(swaps)} swaps...")
        execute_swaps(slots, swaps, config)
        time.sleep(config.after_swap_delay)
        slots = detect_slots(config)

    if not slots:
        print("No items detected after alignment.")
        return False

    adjacency = build_isometric_adjacency(slots, config)
    merges = plan_merge_triggers(
        [slot.label for slot in slots], adjacency, config.max_group_size
    )
    if merges:
        print(f"Merging {len(merges)} groups...")
        execute_merges(slots, merges, config)
    return True


def run_sort_cycle(config):
    slots = detect_slots(config)
    if not slots:
        print("No items detected. Check the game window or lower THRESHOLD.")
        return False

    _, swaps, _ = optimize_isometric_plan(slots, config, strict_sort=True)
    focus_game_window(config)
    if not swaps:
        print("Board is already perfectly sorted!")
        return True

    print(f"Sorting {len(swaps)} swaps...")
    execute_swaps(slots, swaps, config)
    time.sleep(config.after_swap_delay)
    return True


def find_box_btn(screenshot_img, config):
    template_path = config.template_dir / "box_btn.png"
    if not template_path.exists():
        print(f"Error: {template_path} does not exist.")
        return None

    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None:
        print(f"Error: Could not read {template_path}")
        return None

    screenshot_features = matching_features(screenshot_img)
    screenshot_h, screenshot_w = screenshot_img.shape[:2]
    best_score, best_loc = -1.0, None

    for scaled_template in scaled_templates(template, config):
        template_h, template_w = scaled_template.shape[:2]
        if template_h > screenshot_h or template_w > screenshot_w:
            continue

        result = combined_match_score(
            screenshot_features, matching_features(scaled_template), config
        )
        _, score, _, location = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_loc = (
                location[0] + template_w // 2,
                location[1] + template_h // 2,
            )

    return best_loc if best_score >= config.threshold else None


def run_box_btn_cycle(config):
    screenshot_img, offset = capture_game_bgr(config)
    if screenshot_img is None:
        print("No screen captured.")
        return False

    if len(config.template_scales) == 1:
        detected_scale, _ = determine_best_scale(screenshot_img, config)
        config.template_scales = (detected_scale,)

    location = find_box_btn(screenshot_img, config)
    if location is None:
        print("Could not find box_btn template in the viewport.")
        return False

    focus_game_window(config)
    click_x, click_y = location[0] + offset[0], location[1] + offset[1]
    print("Clicking box button 40 times...")
    for _ in range(40):
        pyautogui.click(click_x, click_y)
        time.sleep(0.01)
    return True


HOTKEY_ACTIONS = (
    (0x58, "Auto-farming", run_cycle),
    (0x43, "Sorting", run_sort_cycle),
    (0x5A, "Clicking the box button", run_box_btn_cycle),
)


def run_action(name, action, config):
    play_sound("start")
    print(f"\n{name}...")
    try:
        success = action(config)
    except pyautogui.FailSafeException:
        raise
    except Exception as error:
        print(f"{name} failed: {error}")
        success = False

    play_sound("success" if success else "error")
    return success


def run_hotkey_action(config):
    for key, name, action in HOTKEY_ACTIONS:
        if is_key_pressed(key):
            run_action(name, action, config)
            print("Ready. Focus Discord and press X, C, or Z.")
            time.sleep(1.0)
            return True
    return False


def main():
    config = Config()
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.0

    print("Auto-Farm ready. Focus Discord, then press X to farm, C to sort, or Z to click the box.")
    print("Press Ctrl+C to exit or move the mouse to a screen corner to stop an action.")

    while True:
        try:
            if is_game_window_active(config):
                run_hotkey_action(config)
            time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nExiting Auto-Farm controller. Goodbye!")
            break
        except pyautogui.FailSafeException:
            print("\nFail-safe activated; action stopped. Listening again.")


if __name__ == "__main__":
    main()
