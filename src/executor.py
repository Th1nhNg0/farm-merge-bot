import time
from collections import defaultdict
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
    offsets = defaultdict(list)

    for slot in slots:
        offsets[slot.label].append(
            (
                slot.center[0] - slot.grid_anchor[0],
                slot.center[1] - slot.grid_anchor[1],
            )
        )

    return {
        label: (
            float(np.median([x for x, y in label_offsets])),
            float(np.median([y for x, y in label_offsets])),
        )
        for label, label_offsets in offsets.items()
    }


def slot_click_point(slot, label, click_offsets):
    """Finds where a label is clickable after it has moved to a new slot."""
    if label.startswith("xu_"):
        offset_x, offset_y = 0.0, 0.0
    else:
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


def execute_merges(slots, merge_triggers, config):
    """Executes one merge drag per full group of max_group_size items.

    Drags the from_slot item onto the to_slot item. The game then combines
    all items in the group (the remaining 4 stay connected without the
    dragged item, so the merge triggers correctly).
    """
    click_offsets = label_click_offsets(slots)

    for k, trigger in enumerate(merge_triggers, start=1):
        src_slot = trigger["from_slot"]
        dst_slot = trigger["to_slot"]
        label = trigger["label"]

        src_xy = slot_click_point(slots[src_slot], label, click_offsets)
        dst_xy = slot_click_point(slots[dst_slot], label, click_offsets)

        print(
            f"Merge {k}: drag {label} from slot {src_slot} onto slot {dst_slot}"
        )

        drag_swap(src_xy, dst_xy, config)
        time.sleep(config.after_swap_delay)

