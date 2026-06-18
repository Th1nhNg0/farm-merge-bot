import pyautogui
import time
import cv2
import numpy as np
import heapq

from dataclasses import dataclass
from collections import Counter


# ---------------- config ----------------

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

# Screenshot region: left, top, width, height
REGION = (510, 200, 850, 600)

SCREEN_X0 = REGION[0]
SCREEN_Y0 = REGION[1]

THRESHOLD = 0.70
NMS_IOU = 0.25

# Spatial graph settings
K_NEIGHBORS = 6
ADJ_FACTOR = 2.6

# Swap settings
DRY_RUN = False
MAX_SWAPS = 80
DRAG_DURATION = 0.1
AFTER_SWAP_DELAY = 0.1

# Drawing settings
SHOW_WINDOW = False
DRAW_ARROWS = True
MAX_DRAWN_ARROWS = 30

# Level ordering inside each item group.
# True usually produces shorter swaps.
ORDER_LEVELS_BY_CURRENT_POSITION = True

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


# ---------------- data model ----------------


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


# ---------------- detection ----------------


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
    Removes duplicate overlapping detections globally.
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


# ---------------- basic geometry ----------------


def euclidean(a, b):
    ax, ay = a
    bx, by = b
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def pairwise_distance_matrix(slots):
    centers = np.array([d.center for d in slots], dtype=np.float32)
    diff = centers[:, None, :] - centers[None, :, :]
    return np.sqrt(np.sum(diff * diff, axis=2))


def stable_sort_slots(detections):
    """
    Gives stable slot indices from top-to-bottom, left-to-right.
    The cluster algorithm itself still uses true 2D distance.
    """
    return sorted(detections, key=lambda d: (d.center[1], d.center[0]))


# ---------------- spatial graph ----------------


def build_spatial_graph(slots):
    """
    Builds a nearby-slot graph.

    Each slot connects to several nearby slots. This gives us a notion of
    local adjacency without requiring a perfect rectangular grid.
    """
    n = len(slots)
    dist = pairwise_distance_matrix(slots)

    if n <= 1:
        return [set() for _ in range(n)], dist, 1.0

    nearest_distances = []

    for i in range(n):
        vals = [dist[i, j] for j in range(n) if j != i]
        nearest_distances.append(min(vals))

    median_nn = float(np.median(nearest_distances))
    max_edge = median_nn * ADJ_FACTOR

    adj = [set() for _ in range(n)]

    for i in range(n):
        order = np.argsort(dist[i])
        added = 0

        for j in order:
            j = int(j)

            if i == j:
                continue

            # Always keep at least a couple of neighbors,
            # but avoid connecting very distant objects too aggressively.
            if dist[i, j] <= max_edge or added < 2:
                adj[i].add(j)
                adj[j].add(i)
                added += 1

            if added >= K_NEIGHBORS:
                break

    return adj, dist, median_nn


# ---------------- cluster allocation ----------------


def centroid_of_indices(slots, indices):
    pts = np.array([slots[i].center for i in indices], dtype=np.float32)

    if len(pts) == 0:
        return np.array([0.0, 0.0], dtype=np.float32)

    return np.median(pts, axis=0)


def choose_seed(slots, available, current_item_indices, target_point, dist, median_nn):
    """
    Chooses a seed slot for an item-type cluster.

    It prefers:
    1. slots near where that item type already exists;
    2. slots surrounded by the same item type.
    """
    same_set = set(current_item_indices)
    n = len(slots)

    best_i = None
    best_score = float("inf")

    for i in available:
        center = np.array(slots[i].center, dtype=np.float32)
        d_to_target = float(np.linalg.norm(center - target_point))

        nearest = np.argsort(dist[i])[: min(10, n)]
        same_nearby = sum(1 for j in nearest if int(j) in same_set)

        score = d_to_target

        # Reward positions already occupied by this type.
        if i in same_set:
            score -= median_nn * 0.80

        # Reward neighborhoods that already contain this type.
        score -= median_nn * 0.40 * same_nearby

        if score < best_score:
            best_score = score
            best_i = i

    return best_i


def grow_compact_cluster(seed, size, available, adj, dist):
    """
    Grows a compact connected cluster from a seed.

    The cluster is exact-size, so if an item type has 17 objects,
    it receives exactly 17 target slots.
    """
    if size <= 0:
        return []

    if seed is None:
        return []

    available = set(available)

    cluster = []
    cluster_set = set()

    heap = []
    heapq.heappush(heap, (0.0, seed))

    while heap and len(cluster) < size:
        _, i = heapq.heappop(heap)

        if i not in available:
            continue

        if i in cluster_set:
            continue

        cluster.append(i)
        cluster_set.add(i)

        for nb in adj[i]:
            if nb in available and nb not in cluster_set:
                priority = float(dist[seed, nb])
                heapq.heappush(heap, (priority, nb))

    # Fallback: if graph growth could not fill the group,
    # add nearest remaining available slots.
    while len(cluster) < size:
        remaining = [i for i in available if i not in cluster_set]

        if not remaining:
            break

        if cluster_set:
            best = min(
                remaining, key=lambda r: min(float(dist[r, c]) for c in cluster_set)
            )
        else:
            best = min(remaining, key=lambda r: float(dist[seed, r]))

        cluster.append(best)
        cluster_set.add(best)

    return cluster


def sort_indices_adjacent_path(slots, indices):
    """
    Orders a cluster internally using a local snake path.

    This is used only inside one compact cluster, not across the whole board.
    """
    indices = list(indices)

    if len(indices) <= 1:
        return indices

    median_h = np.median([slots[i].h for i in indices])
    row_tol = max(12, median_h * 0.70)

    rows = []

    for idx in sorted(indices, key=lambda i: slots[i].center[1]):
        _, cy = slots[idx].center
        placed = False

        for row in rows:
            if abs(cy - row["y"]) <= row_tol:
                row["items"].append(idx)
                row["y"] = np.mean([slots[j].center[1] for j in row["items"]])
                placed = True
                break

        if not placed:
            rows.append({"y": cy, "items": [idx]})

    rows.sort(key=lambda r: r["y"])

    ordered = []
    previous_idx = None

    for row in rows:
        left_to_right = sorted(row["items"], key=lambda i: slots[i].center[0])
        right_to_left = list(reversed(left_to_right))

        if previous_idx is None:
            chosen = left_to_right
        else:
            prev_center = slots[previous_idx].center

            dist_ltr = euclidean(prev_center, slots[left_to_right[0]].center)
            dist_rtl = euclidean(prev_center, slots[right_to_left[0]].center)

            chosen = left_to_right if dist_ltr <= dist_rtl else right_to_left

        ordered.extend(chosen)
        previous_idx = chosen[-1]

    return ordered


def choose_level_order_for_item(item, block_path, slots, counts, dist):
    """
    Chooses the level order inside an item-type block.

    Example:
        bo_2 bo_2 bo_2 bo_1 bo_1 bo_3

    is allowed if level 2 is already closer to the beginning of the block.
    This generally reduces swap distance.

    If you want strict 1, 2, 3 order, set:
        ORDER_LEVELS_BY_CURRENT_POSITION = False
    """
    present_levels = [level for level in levels if counts[f"{item}_{level}"] > 0]

    if not ORDER_LEVELS_BY_CURRENT_POSITION:
        return present_levels

    block_rank = {idx: rank for rank, idx in enumerate(block_path)}

    scores = {}

    for level in present_levels:
        label = f"{item}_{level}"
        current_indices = [i for i, d in enumerate(slots) if d.label == label]

        projected_ranks = []

        for ci in current_indices:
            nearest_block_idx = min(block_path, key=lambda b: float(dist[ci, b]))
            projected_ranks.append(block_rank[nearest_block_idx])

        if projected_ranks:
            scores[level] = float(np.median(projected_ranks))
        else:
            scores[level] = 999999.0

    return sorted(present_levels, key=lambda level: scores[level])


def make_clustered_target_labels(slots, adj, dist, median_nn):
    """
    Produces target_labels aligned with slot indices.

    The final arrangement is spatially clustered:

        [bo group]
            [bo_1 subgroup]
            [bo_2 subgroup]
            [bo_3 subgroup]

        [carot group]
            [carot_1 subgroup]
            [carot_2 subgroup]
            [carot_3 subgroup]

    This is better than a single global snake order.
    """
    n = len(slots)
    current_labels = [d.label for d in slots]
    counts = Counter(current_labels)

    target_labels = [None] * n
    available = set(range(n))

    item_indices = {}
    item_sizes = {}

    for item in items:
        idxs = [i for i, d in enumerate(slots) if d.item == item]
        size = len(idxs)

        if size > 0:
            item_indices[item] = idxs
            item_sizes[item] = size

    present_items = list(item_indices.keys())

    # Allocate larger groups first, because large compact groups are harder
    # to fit after small groups fragment the board.
    present_items.sort(
        key=lambda item: (
            -item_sizes[item],
            centroid_of_indices(slots, item_indices[item])[1],
            centroid_of_indices(slots, item_indices[item])[0],
        )
    )

    blocks = {}

    for item in present_items:
        size = item_sizes[item]
        current_idxs = item_indices[item]
        target_point = centroid_of_indices(slots, current_idxs)

        seed = choose_seed(
            slots=slots,
            available=available,
            current_item_indices=current_idxs,
            target_point=target_point,
            dist=dist,
            median_nn=median_nn,
        )

        block = grow_compact_cluster(
            seed=seed, size=size, available=available, adj=adj, dist=dist
        )

        blocks[item] = block
        available -= set(block)

    # Assign level subgroups inside each item-type block.
    for item in present_items:
        block = blocks[item]
        block_path = sort_indices_adjacent_path(slots, block)

        level_order = choose_level_order_for_item(
            item=item, block_path=block_path, slots=slots, counts=counts, dist=dist
        )

        pos = 0

        for level in level_order:
            label = f"{item}_{level}"
            amount = counts[label]

            for _ in range(amount):
                if pos >= len(block_path):
                    break

                slot_idx = block_path[pos]
                target_labels[slot_idx] = label
                pos += 1

    # Defensive fallback.
    for i in range(n):
        if target_labels[i] is None:
            target_labels[i] = current_labels[i]

    return target_labels, blocks


# ---------------- swap planning ----------------


def plan_swaps_nearest(slots, current_labels, target_labels, dist):
    """
    Plans swaps.

    Important difference from the earlier version:
    when a target slot needs a label, this chooses the nearest later slot
    containing that label, instead of the first slot in list order.
    """
    current = current_labels[:]
    swaps = []
    n = len(current)

    for i, wanted_label in enumerate(target_labels):
        if current[i] == wanted_label:
            continue

        candidates = [j for j in range(i + 1, n) if current[j] == wanted_label]

        if not candidates:
            print(f"Warning: no candidate found for slot {i}, label {wanted_label}")
            continue

        def candidate_cost(j):
            cost = float(dist[i, j])

            # Avoid moving an already-correct item unless necessary.
            if current[j] == target_labels[j]:
                cost += 10000.0

            # Prefer direct two-way correction.
            if target_labels[j] == current[i]:
                cost -= 500.0

            return cost

        j = min(candidates, key=candidate_cost)

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


# ---------------- visualization ----------------


def draw_plan(
    img, slots, current_labels, target_labels, swaps, blocks, path="plan.png"
):
    debug = img.copy()

    palette = [
        (0, 255, 0),
        (0, 165, 255),
        (255, 0, 255),
        (255, 255, 0),
        (255, 0, 0),
        (0, 255, 255),
        (180, 120, 255),
        (120, 255, 180),
        (255, 180, 120),
        (180, 255, 120),
    ]

    item_to_color = {}

    for k, item in enumerate(blocks.keys()):
        item_to_color[item] = palette[k % len(palette)]

    # Draw cluster targets.
    for item, indices in blocks.items():
        color = item_to_color[item]

        for idx in indices:
            d = slots[idx]
            x, y, w, h = d.x, d.y, d.w, d.h

            correct = current_labels[idx] == target_labels[idx]

            cv2.rectangle(debug, (x, y), (x + w, y + h), color, 2)

            text = f"{idx}:{current_labels[idx]}->{target_labels[idx]}"
            cv2.putText(
                debug,
                text,
                (x, max(12, y - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                color if not correct else (0, 255, 0),
                1,
                cv2.LINE_AA,
            )

    # Draw only first N arrows; drawing every arrow becomes unreadable.
    if DRAW_ARROWS:
        for k, s in enumerate(swaps[:MAX_DRAWN_ARROWS], start=1):
            src = slots[s["from_slot"]].center
            dst = slots[s["to_slot"]].center

            cv2.arrowedLine(debug, src, dst, (255, 0, 255), 2, tipLength=0.25)

            mx = (src[0] + dst[0]) // 2
            my = (src[1] + dst[1]) // 2

            cv2.putText(
                debug,
                str(k),
                (mx, my),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 0, 255),
                2,
                cv2.LINE_AA,
            )

    cv2.imwrite(path, debug)
    print(f"Saved plan image: {path}")


# ---------------- mouse actions ----------------


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
    Executes the planned swaps.

    Assumption:
        Dragging item A onto item B swaps their positions.
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


def main():
    time.sleep(1)

    im = pyautogui.screenshot(region=REGION)
    im.save("screenshot.png")

    screenshot_img = cv2.imread("screenshot.png", cv2.IMREAD_COLOR)
    assert screenshot_img is not None, "Failed to load screenshot.png"

    detections = detect_all_items(screenshot_img)

    print(f"Detected {len(detections)} items.")

    if not detections:
        print("No items detected. Try lowering THRESHOLD.")
        return

    slots = stable_sort_slots(detections)

    current_labels = [d.label for d in slots]

    adj, dist, median_nn = build_spatial_graph(slots)

    target_labels, blocks = make_clustered_target_labels(
        slots=slots, adj=adj, dist=dist, median_nn=median_nn
    )

    swaps = plan_swaps_nearest(
        slots=slots,
        current_labels=current_labels,
        target_labels=target_labels,
        dist=dist,
    )

    print(f"Planned {len(swaps)} swaps.")

    print("\nItem clusters:")
    for item, indices in blocks.items():
        labels_inside = Counter(target_labels[i] for i in indices)
        print(f"{item}: {len(indices)} slots -> {dict(labels_inside)}")

    print("\nSwap plan:")
    for i, s in enumerate(swaps[:MAX_SWAPS], start=1):
        print(
            f"{i}. slot {s['from_slot']} -> slot {s['to_slot']} | "
            f"{s['moving_label']} swaps with {s['replaced_label']}"
        )

    draw_plan(
        img=screenshot_img,
        slots=slots,
        current_labels=current_labels,
        target_labels=target_labels,
        swaps=swaps,
        blocks=blocks,
        path="plan.png",
    )

    if SHOW_WINDOW:
        preview = cv2.imread("plan.png", cv2.IMREAD_COLOR)
        cv2.imshow("Clustered Swap Plan", preview)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    if DRY_RUN:
        print("\nDRY_RUN is True. Inspect plan.png before enabling real dragging.")
        return

    execute_swaps(slots, swaps)


if __name__ == "__main__":
    main()
