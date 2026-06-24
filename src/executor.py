import time
import numpy as np
import pyautogui


def drag_swap(src_xy, dst_xy, config):
    sx, sy = src_xy
    dx, dy = dst_xy

    # The cursor is not interacting with the game yet, so the automatic pause
    # after this positioning move is redundant. Keep all pauses once the drag
    # begins, where they protect input reliability.
    pyautogui.moveTo(sx, sy, duration=0, _pause=False)
    pyautogui.mouseDown()
    pyautogui.moveTo(dx, dy, duration=config.drag_duration)
    time.sleep(config.swap_settle_delay)
    pyautogui.mouseUp()


def label_click_offsets(slots):
    """Returns each label's median clickable-center offset from a slot anchor."""
    offsets = {}

    for slot in slots:
        offsets.setdefault(slot.label, []).append(
            (
                slot.center[0] - slot.grid_anchor[0],
                slot.center[1] - slot.grid_anchor[1],
            )
        )

    return {
        label: (
            float(np.median([offset[0] for offset in label_offsets])),
            float(np.median([offset[1] for offset in label_offsets])),
        )
        for label, label_offsets in offsets.items()
    }


def slot_click_point(slot, label, click_offsets):
    """Finds where a label is clickable after it has moved to a new slot."""
    offset_x, offset_y = click_offsets.get(label, (0.0, 0.0))
    return (
        int(round(slot.grid_anchor[0] + offset_x)),
        int(round(slot.grid_anchor[1] + offset_y)),
    )


def execute_swaps(slots, swaps, config):
    """
    Executes the planned swaps.

    Assumption:
        Dragging item A onto item B swaps their positions.
    """
    click_offsets = label_click_offsets(slots)

    for k, swap in enumerate(swaps, start=1):
        src_slot = swap["from_slot"]
        dst_slot = swap["to_slot"]

        # Slot anchors stay fixed, while sprite centers depend on the label
        # currently occupying that slot. Use the planned labels so later swaps
        # still click the right sprite after earlier swaps moved it.
        src_xy = slot_click_point(slots[src_slot], swap["moving_label"], click_offsets)
        dst_xy = slot_click_point(
            slots[dst_slot], swap["replaced_label"], click_offsets
        )

        print(
            f"Swap {k}: "
            f"{swap['moving_label']} from slot {src_slot} "
            f"to slot {dst_slot}, replacing {swap['replaced_label']}"
        )

        drag_swap(src_xy, dst_xy, config)
        time.sleep(config.after_swap_delay)
