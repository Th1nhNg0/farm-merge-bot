import time
import argparse
import sys
import pyautogui
import pygetwindow as gw

from src.config import Config
from src.detection import capture_game_bgr, detect_all_items, save_detection_debug_images, save_merge_debug_image, focus_game_window
from src.geometry import stable_sort_slots, build_isometric_adjacency
from src.planner import optimize_isometric_plan, plan_merge_triggers
from src.executor import execute_swaps, execute_merges


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


def clear_key_state(vk_code):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.GetAsyncKeyState(vk_code)


def play_sound(sound_type="info"):
    """Plays auditory cues/notifications so the user knows status while focusing the game window."""
    if sys.platform != "win32":
        sys.stdout.write('\a')
        sys.stdout.flush()
        return
    import winsound
    try:
        if sound_type == "start":
            winsound.Beep(2000, 50)
        elif sound_type == "success":
            winsound.Beep(2000, 50)
            time.sleep(0.05)
            winsound.Beep(2500, 50)
        elif sound_type == "error":
            winsound.Beep(600, 250)
        else:
            winsound.Beep(1000, 100)
    except Exception:
        try:
            winsound.MessageBeep()
        except Exception:
            pass


def cleanup_old_runs(debug_dir, keep_limit=10):
    """Deletes oldest run directories if count exceeds keep_limit to prevent disk bloat."""
    try:
        if not debug_dir.exists():
            return
        run_dirs = sorted(
            [d for d in debug_dir.iterdir() if d.is_dir() and d.name.startswith("run_")],
            key=lambda d: d.stat().st_mtime
        )
        if len(run_dirs) > keep_limit:
            import shutil
            for d in run_dirs[:-keep_limit]:
                try:
                    shutil.rmtree(d)
                    print(f"Cleaned up old debug run directory: {d}")
                except Exception as e:
                    print(f"Warning: Failed to clean up old run directory {d}: {e}")
    except Exception as e:
        print(f"Warning: Failed during old runs cleanup: {e}")


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


def run_cycle(config, suffix=""):
    play_sound("start")
    try:
        # ── Phase 1: detect → plan align swaps → execute ─────────────────────────
        screenshot_img, offset, slots = detect_slots(config, suffix=suffix)

        if not slots:
            print("No items detected. Check the game window or lower THRESHOLD.")
            play_sound("error")
            return False

        print("Planning...")
        started = time.perf_counter()
        _, phase1_swaps, _ = optimize_isometric_plan(slots, config)
        print(f"Done in {time.perf_counter() - started:.2f}s: {len(phase1_swaps)} align swaps.")

        focus_game_window(config)

        if phase1_swaps:
            print(f"\nPhase 1: aligning ({len(phase1_swaps)} swaps)...")
            execute_swaps(slots, phase1_swaps, config)
            time.sleep(config.after_swap_delay)

        # ── Phase 2: fresh detect → plan merge triggers → execute ─────────────────
        screenshot_img, offset, slots = detect_slots(config, save_debug=True, suffix=f"_after_phase1{suffix}")

        if not slots:
            print("Phase 2: no items detected after Phase 1.")
            play_sound("error")
            return True

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
            
        play_sound("success")
        return True
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        print(f"\n[Error during run cycle] {e}")
        import traceback
        traceback.print_exc()
        play_sound("error")
        return False


def run_sort_cycle(config, suffix=""):
    play_sound("start")
    try:
        screenshot_img, offset, slots = detect_slots(config, suffix=suffix)

        if not slots:
            print("No items detected. Check the game window or lower THRESHOLD.")
            play_sound("error")
            return False

        print("Planning strict sort...")
        started = time.perf_counter()
        _, phase1_swaps, _ = optimize_isometric_plan(slots, config, strict_sort=True)
        print(f"Done in {time.perf_counter() - started:.2f}s: {len(phase1_swaps)} sorting swaps.")

        focus_game_window(config)

        if phase1_swaps:
            print(f"\nSorting ({len(phase1_swaps)} swaps)...")
            execute_swaps(slots, phase1_swaps, config)
            time.sleep(config.after_swap_delay)
        else:
            print("Board is already perfectly sorted!")
            
        play_sound("success")
        return True
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        print(f"\n[Error during sort cycle] {e}")
        import traceback
        traceback.print_exc()
        play_sound("error")
        return False






def main(args=None):
    if args is None:
        # Check if we are running in a unit test runner (unittest / pytest)
        is_testing = any("unittest" in arg or "pytest" in arg or "discover" in arg for arg in sys.argv)
        if is_testing:
            args = []
        else:
            args = sys.argv[1:]
    else:
        is_testing = False

    parser = argparse.ArgumentParser(description="Auto-farm bot for game viewport.")
    parser.add_argument(
        "--loop", "-l",
        action="store_true",
        help="Run continuously in a loop once triggered by pressing 'X' (press ESC to pause loop)"
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=1.0,
        help="Delay in seconds between loops in continuous mode"
    )
    parser.add_argument(
        "--window", "-w",
        type=str,
        default=None,
        help="Title of the window to target (defaults to auto-detecting Discord)"
    )
    parsed_args = parser.parse_args(args)

    config = Config()
    if parsed_args.window:
        config.window_title = parsed_args.window

    # Archive debug logs in unique run subdirectories to enable future analysis
    if is_testing:
        cleanup_old_runs(config.detection_debug_dir, keep_limit=10)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        config.detection_debug_dir = config.detection_debug_dir / f"run_{timestamp}"
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.0
        run_cycle(config)
        return

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.0

    print("==================================================")
    print("Auto-Farm Controller Initialized")
    print(f"Mode: {'Continuous Loop (press ESC to pause)' if parsed_args.loop else 'Single Run'}")
    if parsed_args.loop:
        print(f"Loop Delay: {parsed_args.delay} seconds")
    print("--------------------------------------------------")
    print("Instructions:")
    print("  1. Make sure your Discord/game window is visible.")
    print("  2. Focus the Discord/game window.")
    print("  3. Press the 'X' key to start autorunning.")
    print("  4. Press the 'C' key to strictly sort all items.")
    print("  5. To stop: press Ctrl+C in this terminal, or")
    print("     move your mouse cursor to any corner of the screen (FailSafe).")
    print("==================================================")

    while True:
        try:
            if is_game_window_active(config):
                # VK_X = 0x58
                if is_key_pressed(0x58):
                    print("\n[Triggered] 'X' pressed while Discord is active. Starting autorun...")
                    
                    if parsed_args.loop:
                        print("Entering continuous loop. Press 'ESC' key to pause.")
                        clear_key_state(0x1B)  # VK_ESCAPE
                        clear_key_state(0x58)  # VK_X
                        
                        while True:
                            if is_key_pressed(0x1B):  # VK_ESCAPE
                                print("[Paused] 'ESC' pressed. Pausing loop. Press 'X' while in Discord to resume.")
                                time.sleep(1.0)
                                break
                            
                            cleanup_old_runs(config.detection_debug_dir, keep_limit=10)
                            timestamp = time.strftime("%Y%m%d_%H%M%S")
                            run_debug_dir = config.detection_debug_dir / f"run_{timestamp}"
                            orig_debug_dir = config.detection_debug_dir
                            config.detection_debug_dir = run_debug_dir
                            
                            try:
                                run_cycle(config)
                            finally:
                                config.detection_debug_dir = orig_debug_dir
                                
                            print(f"Waiting {parsed_args.delay}s before next cycle... (press ESC to pause)")
                            
                            # Sleep in small increments to check for ESC key during sleep
                            sleep_start = time.perf_counter()
                            paused = False
                            while time.perf_counter() - sleep_start < parsed_args.delay:
                                if is_key_pressed(0x1B):
                                    print("[Paused] 'ESC' pressed. Pausing loop. Press 'X' while in Discord to resume.")
                                    paused = True
                                    break
                                time.sleep(0.05)
                            if paused:
                                time.sleep(1.0)
                                break
                    else:
                        cleanup_old_runs(config.detection_debug_dir, keep_limit=10)
                        timestamp = time.strftime("%Y%m%d_%H%M%S")
                        orig_debug_dir = config.detection_debug_dir
                        config.detection_debug_dir = config.detection_debug_dir / f"run_{timestamp}"
                        try:
                            run_cycle(config)
                        finally:
                            config.detection_debug_dir = orig_debug_dir
                        print("Run completed. Focus Discord and press 'X' to run again.")
                        time.sleep(1.0)
                
                # VK_C = 0x43
                elif is_key_pressed(0x43):
                    print("\n[Triggered] 'C' pressed while Discord is active. Starting board sorting...")
                    clear_key_state(0x43)
                    
                    cleanup_old_runs(config.detection_debug_dir, keep_limit=10)
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    orig_debug_dir = config.detection_debug_dir
                    config.detection_debug_dir = config.detection_debug_dir / f"run_sort_{timestamp}"
                    try:
                        run_sort_cycle(config)
                    finally:
                        config.detection_debug_dir = orig_debug_dir
                    print("Sorting completed. Focus Discord and press 'X' to run, or 'C' to sort.")
                    time.sleep(1.0)
            
            time.sleep(0.1)
            
        except KeyboardInterrupt:
            print("\nExiting Auto-Farm controller. Goodbye!")
            break
        except pyautogui.FailSafeException:
            print("\n[FailSafe] Mouse moved to corner. Aborting run/loop.")
            time.sleep(1.0)
            print("Listening again. Focus Discord and press 'X' to run, or 'C' to sort.")


if __name__ == "__main__":
    main()
