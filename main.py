import pyautogui
import time
import cv2
import numpy as np
from dataclasses import dataclass
from collections import Counter

items = [
    "bo",
    "carot",
    "daunanh",
    "ga",
    "heo",
    "huongduong",
    "luami",
    "mia",
    "de",
    "bap",
]

levels = [1, 2, 3]

# IMPORTANT: these must match your screenshot crop
REGION = (510, 200, 850, 600)  # left, top, width, height
SCREEN_X0, SCREEN_Y0 = REGION[0], REGION[1]

THRESHOLD = 0.70
NMS_IOU = 0.25

DRY_RUN = False  # first run with True
MAX_SWAPS = 1000  # safety limit
DRAG_DURATION = 0.2
AFTER_SWAP_DELAY = 0.1

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


@dataclass
class Detection:
    label: str
    item: str
    level: int
    x: int
    y: int
    w: int
    h: int
    score: float

    @property
    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def screen_center(self):
        cx, cy = self.center
        return SCREEN_X0 + cx, SCREEN_Y0 + cy


def detect_all_items(screenshot_img):
    screenshot_hsv = cv2.cvtColor(screenshot_img, cv2.COLOR_BGR2HSV)

    detections = []

    for item in items:
        for level in levels:
            template_path = f"images/{item}{level}.png"
            template = cv2.imread(template_path, cv2.IMREAD_COLOR)

            if template is None:
                print(f"Skipping missing template: {template_path}")
                continue

            template_hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
            th, tw = template_hsv.shape[:2]

            result = cv2.matchTemplate(
                screenshot_hsv, template_hsv, cv2.TM_CCOEFF_NORMED
            )

            # Keep only local maxima above threshold.
            # This avoids creating thousands of nearly identical boxes.
            local_max = result == cv2.dilate(result, np.ones((3, 3), np.uint8))
            ys, xs = np.where((result >= THRESHOLD) & local_max)

            for x, y in zip(xs, ys):
                detections.append(
                    Detection(
                        label=f"{item}_{level}",
                        item=item,
                        level=level,
                        x=int(x),
                        y=int(y),
                        w=int(tw),
                        h=int(th),
                        score=float(result[y, x]),
                    )
                )

    return nms_detections(detections)


def nms_detections(detections):
    """
    Remove overlapping detections globally.
    This is better than grouping rectangles separately for each template,
    because different templates may detect the same visual object.
    """
    if not detections:
        return []

    boxes = [[d.x, d.y, d.w, d.h] for d in detections]
    scores = [d.score for d in detections]

    indices = cv2.dnn.NMSBoxes(
        boxes, scores, score_threshold=0.0, nms_threshold=NMS_IOU
    )

    if len(indices) == 0:
        return []

    indices = np.array(indices).flatten()
    return [detections[i] for i in indices]


def sort_slots(detections):
    """
    Sort detections into visual rows, then left-to-right inside each row.

    This gives a stable slot order:
    slot 0, slot 1, slot 2, ...
    """
    if not detections:
        return []

    median_h = np.median([d.h for d in detections])
    row_tol = max(12, median_h * 0.65)

    rows = []

    for d in sorted(detections, key=lambda z: z.center[1]):
        cx, cy = d.center
        placed = False

        for row in rows:
            if abs(cy - row["y"]) <= row_tol:
                row["items"].append(d)
                row["y"] = np.mean([z.center[1] for z in row["items"]])
                placed = True
                break

        if not placed:
            rows.append({"y": cy, "items": [d]})

    rows.sort(key=lambda r: r["y"])

    ordered = []
    for row in rows:
        ordered.extend(sorted(row["items"], key=lambda z: z.center[0]))

    return ordered


def make_target_labels(slots):
    """
    Produce desired label order.

    Example:
    current labels:
        bo_1, mia_2, bo_1, carot_1, mia_2

    target labels:
        bo_1, bo_1, carot_1, mia_2, mia_2
    """
    counts = Counter(d.label for d in slots)

    target = []
    for item in items:
        for level in levels:
            label = f"{item}_{level}"
            target.extend([label] * counts[label])

    return target


def plan_swaps(current_labels, target_labels):
    """
    Create swaps assuming dragging item A onto item B swaps their positions.
    """
    current = current_labels[:]
    swaps = []

    for i, wanted_label in enumerate(target_labels):
        if current[i] == wanted_label:
            continue

        # Find a later slot containing the label we need here.
        candidates = [
            j
            for j in range(i + 1, len(current))
            if current[j] == wanted_label and current[j] != target_labels[j]
        ]

        if not candidates:
            candidates = [
                j for j in range(i + 1, len(current)) if current[j] == wanted_label
            ]

        if not candidates:
            continue

        j = candidates[0]

        swaps.append(
            {
                "from_slot": j,
                "to_slot": i,
                "moving_label": current[j],
                "replaced_label": current[i],
            }
        )

        current[i], current[j] = current[j], current[i]

    return swaps


def draw_plan(img, slots, target_labels, swaps, path="plan.png"):
    debug = img.copy()

    for idx, d in enumerate(slots):
        x, y, w, h = d.x, d.y, d.w, d.h

        correct = d.label == target_labels[idx]
        color = (0, 255, 0) if correct else (0, 165, 255)

        cv2.rectangle(debug, (x, y), (x + w, y + h), color, 2)

        text = f"{idx}: {d.label}->{target_labels[idx]}"
        cv2.putText(
            debug,
            text,
            (x, max(12, y - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            color,
            1,
            cv2.LINE_AA,
        )

    for k, s in enumerate(swaps[:MAX_SWAPS]):
        a = slots[s["from_slot"]].center
        b = slots[s["to_slot"]].center

        cv2.arrowedLine(debug, a, b, (255, 0, 255), 2, tipLength=0.25)

        mx = (a[0] + b[0]) // 2
        my = (a[1] + b[1]) // 2

        cv2.putText(
            debug,
            str(k + 1),
            (mx, my),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 0, 255),
            2,
            cv2.LINE_AA,
        )

    cv2.imwrite(path, debug)


def drag_swap(src_xy, dst_xy):
    sx, sy = src_xy
    dx, dy = dst_xy

    pyautogui.moveTo(sx, sy, duration=0.08)
    pyautogui.mouseDown()
    pyautogui.moveTo(dx, dy, duration=DRAG_DURATION)
    time.sleep(0.05)
    pyautogui.mouseUp()


def execute_swaps(slots, swaps):
    """
    Execute the planned swaps.

    The slot coordinates remain fixed. After each drag, we update only our
    internal labels conceptually through the swap plan.
    """
    for k, s in enumerate(swaps[:MAX_SWAPS], start=1):
        src_slot = s["from_slot"]
        dst_slot = s["to_slot"]

        src_xy = slots[src_slot].screen_center
        dst_xy = slots[dst_slot].screen_center

        print(
            f"Swap {k}: "
            f"{s['moving_label']} from slot {src_slot} "
            f"to slot {dst_slot}, replacing {s['replaced_label']}"
        )

        drag_swap(src_xy, dst_xy)
        time.sleep(AFTER_SWAP_DELAY)


# ---------------- main ----------------

time.sleep(1)

im = pyautogui.screenshot(region=REGION)
im.save("screenshot.png")

screenshot_img = cv2.imread("screenshot.png", cv2.IMREAD_COLOR)
assert screenshot_img is not None, "Failed to load screenshot image."

detections = detect_all_items(screenshot_img)

print(f"Detected {len(detections)} items.")

slots = sort_slots(detections)
current_labels = [d.label for d in slots]
target_labels = make_target_labels(slots)

swaps = plan_swaps(current_labels, target_labels)

print(f"Planned {len(swaps)} swaps.")

for i, s in enumerate(swaps[:MAX_SWAPS], start=1):
    print(
        f"{i}. slot {s['from_slot']} -> slot {s['to_slot']} | "
        f"{s['moving_label']} swaps with {s['replaced_label']}"
    )

draw_plan(screenshot_img, slots, target_labels, swaps, path="plan.png")

if DRY_RUN:
    print("DRY_RUN is True. Check plan.png before enabling real dragging.")
else:
    execute_swaps(slots, swaps)
