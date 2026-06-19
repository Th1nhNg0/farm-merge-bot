import argparse
import csv
import itertools
import random
import time
from itertools import zip_longest
import cv2
import numpy as np
import pyautogui

from dataclasses import dataclass
from collections import Counter
from pathlib import Path


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
    "go",
    "da",
    "congcu",
]

levels = [1, 2, 3]
item_levels = {"go": [1, 2, 3, 4, 5], "da": [1, 2, 3, 4, 5], "congcu": [1, 2, 3, 4, 5]}

# Detection settings
THRESHOLD = 0.70
TEMPLATE_DIR = Path("images")
TEMPLATE_SCALES = (0.90, 0.95, 1.00, 1.05, 1.10)
TEMPLATE_THRESHOLDS = {
    # The single-log sprite has less structure than larger objects. All three
    # visible board instances score between 0.62 and 0.65 on the real frame.
    "go_1": 0.62,
}
GRAYSCALE_SCORE_WEIGHT = 0.70
EDGE_SCORE_WEIGHT = 0.30
LOCAL_MAX_KERNEL = 5
DUPLICATE_CENTER_FACTOR = 0.35
MIN_DUPLICATE_CENTER_DISTANCE = 6.0
DETECTION_DEBUG_DIR = Path("debug")

# Orthogonal layout settings
ORTHOGONAL_AXIS_TOL_FACTOR = 0.60
EXACT_LABEL_ORDER_LIMIT = 8
LABEL_ORDER_TRIALS = 512
LABEL_ORDER_SEED = 20260619
ORTHOGONAL_MAX_STEP_FACTOR = 1.45
CONNECTED_REGION_TRIALS = 256
COMPACTNESS_FINALISTS = 6

# Swap settings
DRY_RUN = False
DRAG_DURATION = 0.05
AFTER_SWAP_DELAY = 0.005

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
        return self.center


# ---------------- detection ----------------


def template_paths(item, level):
    """Returns every template variant for an item level."""
    base_name = f"{item}{level}"
    return sorted(
        path
        for path in TEMPLATE_DIR.glob(f"{base_name}*.png")
        if path.stem == base_name or path.stem.startswith(f"{base_name}_")
    )


def matching_features(image):
    """Builds illumination-tolerant intensity and shape features."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 45, 135)
    return gray, edges


def combined_match_score(screenshot_features, template_img):
    template_gray, template_edges = matching_features(template_img)
    screenshot_gray, screenshot_edges = screenshot_features

    gray_score = cv2.matchTemplate(
        screenshot_gray,
        template_gray,
        cv2.TM_CCOEFF_NORMED,
    )
    edge_score = cv2.matchTemplate(
        screenshot_edges,
        template_edges,
        cv2.TM_CCOEFF_NORMED,
    )
    edge_score = np.nan_to_num(edge_score, nan=0.0, posinf=0.0, neginf=0.0)

    return GRAYSCALE_SCORE_WEIGHT * gray_score + EDGE_SCORE_WEIGHT * edge_score


def scaled_templates(template):
    """Yields unique configured template sizes, including the original size."""
    original_h, original_w = template.shape[:2]
    seen_sizes = set()

    for scale in TEMPLATE_SCALES:
        width = max(3, int(round(original_w * scale)))
        height = max(3, int(round(original_h * scale)))

        if (width, height) in seen_sizes:
            continue

        seen_sizes.add((width, height))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        yield cv2.resize(template, (width, height), interpolation=interpolation)


def deduplicate_detections(detections):
    """Keeps the best label near each center without removing nearby slots."""
    kept = []

    for candidate in sorted(detections, key=lambda d: d.score, reverse=True):
        duplicate = False

        for accepted in kept:
            dx = candidate.center[0] - accepted.center[0]
            dy = candidate.center[1] - accepted.center[1]
            center_distance = float(np.hypot(dx, dy))
            reference_size = min(
                candidate.w,
                candidate.h,
                accepted.w,
                accepted.h,
            )
            duplicate_distance = max(
                MIN_DUPLICATE_CENTER_DISTANCE,
                reference_size * DUPLICATE_CENTER_FACTOR,
            )

            if center_distance <= duplicate_distance:
                duplicate = True
                break

        if not duplicate:
            kept.append(candidate)

    return kept


def detect_all_items(screenshot_img, diagnostics=None):
    screenshot_features = matching_features(screenshot_img)
    screenshot_h, screenshot_w = screenshot_img.shape[:2]
    detections = []

    for item in items:
        for level in item_levels.get(item, levels):
            paths = template_paths(item, level)

            if not paths:
                print(
                    "Skipping missing templates: "
                    f"{TEMPLATE_DIR / f'{item}{level}.png'} or "
                    f"{TEMPLATE_DIR / f'{item}{level}_<variant>.png'}"
                )
                continue

            label = f"{item}_{level}"
            threshold = TEMPLATE_THRESHOLDS.get(label, THRESHOLD)

            if diagnostics is not None:
                diagnostics[label] = {
                    "best_score": float("-inf"),
                    "best_template": "",
                    "best_width": 0,
                    "best_height": 0,
                    "best_x": 0,
                    "best_y": 0,
                    "threshold": threshold,
                    "detected_count": 0,
                }

            for template_path in paths:
                template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)

                if template is None:
                    print(f"Skipping unreadable template: {template_path}")
                    continue

                for scaled_template in scaled_templates(template):
                    th, tw = scaled_template.shape[:2]

                    if th > screenshot_h or tw > screenshot_w:
                        continue

                    result = combined_match_score(
                        screenshot_features,
                        scaled_template,
                    )
                    if diagnostics is not None:
                        best_score = float(np.max(result))

                        if best_score > diagnostics[label]["best_score"]:
                            best_y, best_x = np.unravel_index(
                                int(np.argmax(result)),
                                result.shape,
                            )
                            diagnostics[label].update(
                                {
                                    "best_score": best_score,
                                    "best_template": template_path.name,
                                    "best_width": tw,
                                    "best_height": th,
                                    "best_x": int(best_x),
                                    "best_y": int(best_y),
                                }
                            )

                    local_max = result == cv2.dilate(
                        result,
                        np.ones((LOCAL_MAX_KERNEL, LOCAL_MAX_KERNEL), np.uint8),
                    )
                    ys, xs = np.where((result >= threshold) & local_max)

                    for x, y in zip(xs, ys):
                        detections.append(
                            Detection(
                                label=label,
                                item=item,
                                level=level,
                                x=int(x),
                                y=int(y),
                                w=int(tw),
                                h=int(th),
                                score=float(result[y, x]),
                            )
                        )

    detections = deduplicate_detections(detections)

    if diagnostics is not None:
        detected_counts = Counter(detection.label for detection in detections)

        for label, values in diagnostics.items():
            values["detected_count"] = detected_counts[label]

    return detections


def save_detection_debug_images(screenshot_img, detections, diagnostics=None):
    """Saves the captured board and an annotated copy for calibration."""
    DETECTION_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = DETECTION_DEBUG_DIR / "board.png"
    annotated_path = DETECTION_DEBUG_DIR / "detections.png"
    scores_path = DETECTION_DEBUG_DIR / "scores.csv"
    annotated = screenshot_img.copy()
    for detection in detections:
        top_left = (detection.x, detection.y)
        bottom_right = (detection.x + detection.w, detection.y + detection.h)
        cv2.rectangle(annotated, top_left, bottom_right, (0, 255, 0), 1)
        cv2.putText(
            annotated,
            f"{detection.label} {detection.score:.2f}",
            (detection.x, max(10, detection.y - 3)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.32,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.imwrite(str(raw_path), screenshot_img)
    cv2.imwrite(str(annotated_path), annotated)

    if diagnostics is not None:
        fieldnames = [
            "label",
            "best_score",
            "threshold",
            "detected_count",
            "best_template",
            "best_width",
            "best_height",
            "best_x",
            "best_y",
        ]

        with scores_path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()

            for label in sorted(diagnostics):
                values = diagnostics[label]
                writer.writerow(
                    {
                        "label": label,
                        "best_score": f"{values['best_score']:.4f}",
                        "threshold": f"{values['threshold']:.4f}",
                        "detected_count": values["detected_count"],
                        "best_template": values["best_template"],
                        "best_width": values["best_width"],
                        "best_height": values["best_height"],
                        "best_x": values["best_x"],
                        "best_y": values["best_y"],
                    }
                )

    return raw_path, annotated_path, scores_path


# ---------------- geometry ----------------


def pairwise_distance_matrix(slots):
    centers = np.array([d.center for d in slots], dtype=np.float32)
    diff = centers[:, None, :] - centers[None, :, :]
    return np.sqrt(np.sum(diff * diff, axis=2))


def stable_sort_slots(detections):
    """
    Gives stable slot indices from top-to-bottom, left-to-right.

    Consecutive target labels in this order are treated as adjacent items.
    """
    return sorted(detections, key=lambda d: (d.center[1], d.center[0]))


def build_orthogonal_adjacency(slots):
    """Connects each slot to its nearest top, right, bottom, and left slot."""
    adjacency = {index: set() for index in range(len(slots))}

    if len(slots) <= 1:
        return adjacency

    median_width = float(np.median([slot.w for slot in slots]))
    median_height = float(np.median([slot.h for slot in slots]))
    x_tolerance = max(4.0, median_width * ORTHOGONAL_AXIS_TOL_FACTOR)
    y_tolerance = max(4.0, median_height * ORTHOGONAL_AXIS_TOL_FACTOR)
    centers = np.array([slot.center for slot in slots], dtype=np.float32)
    distances = np.linalg.norm(centers[:, None, :] - centers[None, :, :], axis=2)
    np.fill_diagonal(distances, np.inf)
    median_nearest = float(np.median(np.min(distances, axis=1)))
    max_step = max(
        median_width * 1.75,
        median_height * 1.75,
        median_nearest * ORTHOGONAL_MAX_STEP_FACTOR,
    )

    for index, slot in enumerate(slots):
        x, y = slot.center
        directions = {"top": [], "right": [], "bottom": [], "left": []}

        for other_index, other in enumerate(slots):
            if index == other_index:
                continue

            other_x, other_y = other.center
            dx = other_x - x
            dy = other_y - y

            if abs(dx) <= x_tolerance and -max_step <= dy < 0:
                directions["top"].append((abs(dy), other_index))
            if abs(dy) <= y_tolerance and 0 < dx <= max_step:
                directions["right"].append((abs(dx), other_index))
            if abs(dx) <= x_tolerance and 0 < dy <= max_step:
                directions["bottom"].append((abs(dy), other_index))
            if abs(dy) <= y_tolerance and -max_step <= dx < 0:
                directions["left"].append((abs(dx), other_index))

        for candidates in directions.values():
            if not candidates:
                continue

            _, neighbor = min(candidates)
            adjacency[index].add(neighbor)
            adjacency[neighbor].add(index)

    return adjacency


def connected_components(adjacency):
    """Returns connected slot-index sets, largest first."""
    remaining = set(adjacency)
    components = []

    while remaining:
        pending = [min(remaining)]
        component = set()

        while pending:
            index = pending.pop()

            if index in component:
                continue

            component.add(index)
            pending.extend(adjacency[index] - component)

        remaining -= component
        components.append(component)

    return sorted(components, key=lambda component: (-len(component), min(component)))


def largest_orthogonal_component(slots):
    """Removes isolated full-screen matches outside the main item grid."""
    if not slots:
        return []

    adjacency = build_orthogonal_adjacency(slots)
    component = connected_components(adjacency)[0]
    return [slot for index, slot in enumerate(slots) if index in component]


def _axis_groups(slots, primary_axis, tolerance):
    """Groups slots into visually aligned rows or columns."""
    groups = []

    for index in sorted(
        range(len(slots)),
        key=lambda i: (slots[i].center[primary_axis], slots[i].center[1 - primary_axis]),
    ):
        coordinate = slots[index].center[primary_axis]

        if not groups:
            groups.append([index])
            continue

        group_coordinate = float(
            np.median([slots[i].center[primary_axis] for i in groups[-1]])
        )

        if abs(coordinate - group_coordinate) <= tolerance:
            groups[-1].append(index)
        else:
            groups.append([index])

    return groups


def orthogonal_scan_orders(slots, adjacency):
    """Returns row and column snake orders for connected-segment allocation."""
    if not slots:
        return [()]

    median_width = float(np.median([slot.w for slot in slots]))
    median_height = float(np.median([slot.h for slot in slots]))
    row_groups = _axis_groups(
        slots,
        primary_axis=1,
        tolerance=max(4.0, median_height * ORTHOGONAL_AXIS_TOL_FACTOR),
    )
    column_groups = _axis_groups(
        slots,
        primary_axis=0,
        tolerance=max(4.0, median_width * ORTHOGONAL_AXIS_TOL_FACTOR),
    )
    candidates = []

    for groups, secondary_axis in ((row_groups, 0), (column_groups, 1)):
        for reverse_groups in (False, True):
            ordered_groups = list(reversed(groups)) if reverse_groups else groups

            for reverse_first_group in (False, True):
                order = []

                for group_index, group in enumerate(ordered_groups):
                    reverse = reverse_first_group != (group_index % 2 == 1)
                    ordered = sorted(
                        group,
                        key=lambda i: slots[i].center[secondary_axis],
                        reverse=reverse,
                    )
                    order.extend(ordered)

                candidates.append(tuple(order))

        for band_size in (2, 3, 4):
            order = []

            for band_start in range(0, len(groups), band_size):
                band = [
                    sorted(group, key=lambda i: slots[i].center[secondary_axis])
                    for group in groups[band_start : band_start + band_size]
                ]

                for position, column in enumerate(zip_longest(*band)):
                    entries = [index for index in column if index is not None]

                    if position % 2:
                        entries.reverse()

                    order.extend(entries)

            candidates.extend((tuple(order), tuple(reversed(order))))

    return list(dict.fromkeys(candidates))


def candidate_label_orders(current_labels):
    """Yields exhaustive small-board orders and deterministic large-board trials."""
    labels = tuple(dict.fromkeys(current_labels))

    if len(labels) <= EXACT_LABEL_ORDER_LIMIT:
        return itertools.permutations(labels)

    counts = Counter(current_labels)
    candidates = [
        labels,
        tuple(reversed(labels)),
        tuple(sorted(labels)),
        tuple(sorted(labels, key=lambda label: (-counts[label], label))),
        tuple(sorted(labels, key=lambda label: (counts[label], label))),
    ]
    rng = random.Random(LABEL_ORDER_SEED)

    for _ in range(LABEL_ORDER_TRIALS):
        shuffled = list(labels)
        rng.shuffle(shuffled)
        candidates.append(tuple(shuffled))

    return iter(dict.fromkeys(candidates))


def target_labels_for_scan(current_labels, scan_order, label_order):
    """Places each label in one connected segment of an orthogonal scan path."""
    counts = Counter(current_labels)
    target = [None] * len(current_labels)
    offset = 0

    for label in label_order:
        for slot_index in scan_order[offset : offset + counts[label]]:
            target[slot_index] = label
        offset += counts[label]

    return target


def candidate_targets_for_scan(current_labels, scan_order, adjacency, rng):
    """Builds targets whose group boundaries cover every broken snake edge."""
    label_counts = Counter(current_labels)
    remaining_sizes = Counter(label_counts.values())
    size_orders = []
    failed_states = set()

    def search(offset, sizes):
        state = (offset, tuple(sorted(sizes.items())))

        if state in failed_states or len(size_orders) >= 16:
            return False
        if not sizes:
            size_orders.append(())
            return True

        found = False
        options = list(sizes)
        rng.shuffle(options)

        for size in options:
            segment = scan_order[offset : offset + size]

            if not all(
                right in adjacency[left]
                for left, right in zip(segment, segment[1:])
            ):
                continue

            next_sizes = sizes.copy()
            next_sizes[size] -= 1

            if next_sizes[size] == 0:
                del next_sizes[size]

            before = len(size_orders)
            search(offset + size, next_sizes)

            for index in range(before, len(size_orders)):
                size_orders[index] = (size,) + size_orders[index]

            found = found or len(size_orders) > before

        if not found:
            failed_states.add(state)

        return found

    search(0, remaining_sizes)

    for size_order in size_orders:
        segments_by_size = {}
        offset = 0

        for size in size_order:
            segments_by_size.setdefault(size, []).append(
                scan_order[offset : offset + size]
            )
            offset += size

        for trial in range(8):
            target = [None] * len(current_labels)

            for size, segments in segments_by_size.items():
                available_labels = [
                    label for label, count in label_counts.items() if count == size
                ]

                for segment in segments:
                    if trial == 0:
                        label = max(
                            available_labels,
                            key=lambda candidate: sum(
                                current_labels[index] == candidate for index in segment
                            ),
                        )
                    else:
                        label = rng.choice(available_labels)

                    available_labels.remove(label)

                    for index in segment:
                        target[index] = label

            yield target


def grow_connected_target(current_labels, adjacency, rng):
    """Grows one cardinally connected region per label from current-label seeds."""
    counts = Counter(current_labels)
    label_order = list(counts)
    rng.shuffle(label_order)
    target = [None] * len(current_labels)
    regions = {}
    remaining = {}

    for label in label_order:
        seed_options = [
            index
            for index, current_label in enumerate(current_labels)
            if current_label == label and target[index] is None
        ]
        seed = rng.choice(seed_options)
        target[seed] = label
        regions[label] = {seed}
        remaining[label] = counts[label] - 1

    while any(value is None for value in target):
        candidates = []

        for label in label_order:
            if remaining[label] <= 0:
                continue

            frontier = set().union(*(adjacency[index] for index in regions[label]))

            for index in frontier:
                if target[index] is not None:
                    continue

                matching_neighbors = len(adjacency[index] & regions[label])
                candidates.append(
                    (
                        current_labels[index] == label,
                        matching_neighbors,
                        rng.random(),
                        label,
                        index,
                    )
                )

        if not candidates:
            return None

        _, _, _, label, index = max(candidates)
        target[index] = label
        regions[label].add(index)
        remaining[label] -= 1

    return target


def _removal_keeps_connected(indices, removed_index, adjacency):
    """Checks whether removing one slot leaves the remaining subgraph connected."""
    remaining = indices - {removed_index}

    if len(remaining) <= 1:
        return True

    pending = [next(iter(remaining))]
    visited = set()

    while pending:
        index = pending.pop()

        if index in visited:
            continue

        visited.add(index)
        pending.extend((adjacency[index] & remaining) - visited)

    return visited == remaining


def peel_connected_target(current_labels, adjacency, rng):
    """Peels connected label regions while keeping the remainder connected."""
    counts = Counter(current_labels)
    label_order = list(counts)
    rng.shuffle(label_order)
    target = [None] * len(current_labels)
    available = set(range(len(current_labels)))

    for label in label_order[:-1]:
        region = set()

        for _ in range(counts[label]):
            if region:
                candidates = set().union(
                    *(adjacency[index] & available for index in region)
                )
            else:
                candidates = set(available)

            removable = [
                index
                for index in candidates
                if _removal_keeps_connected(available, index, adjacency)
            ]

            if not removable:
                return None

            index = max(
                removable,
                key=lambda candidate: (
                    current_labels[candidate] == label,
                    len(adjacency[candidate] & region),
                    -len(adjacency[candidate] & available),
                    rng.random(),
                ),
            )
            target[index] = label
            region.add(index)
            available.remove(index)

    final_label = label_order[-1]

    if len(available) != counts[final_label]:
        return None

    for index in available:
        target[index] = final_label

    return target


# ---------------- swap planning ----------------


def _shortest_label_cycles(edge_slots):
    """Returns shortest mismatch cycles using polynomial breadth-first paths."""
    adjacency = {}

    for source_label, target_label in edge_slots:
        adjacency.setdefault(source_label, set()).add(target_label)

    cycles = set()
    shortest_length = None

    for start_label, next_label in sorted(edge_slots):
        queue = [next_label]
        parent = {next_label: None}
        head = 0

        while head < len(queue) and start_label not in parent:
            label = queue[head]
            head += 1

            for following_label in sorted(adjacency.get(label, ())):
                if following_label not in parent:
                    parent[following_label] = label
                    queue.append(following_label)

        if start_label not in parent:
            continue

        path = []
        label = start_label

        while label is not None:
            path.append(label)
            label = parent[label]

        path.reverse()
        cycle_labels = (start_label,) + tuple(path[:-1])
        cycle_edges = tuple(
            (cycle_labels[i], cycle_labels[(i + 1) % len(cycle_labels)])
            for i in range(len(cycle_labels))
        )
        rotations = [
            cycle_edges[i:] + cycle_edges[:i] for i in range(len(cycle_edges))
        ]
        canonical = min(rotations)
        length = len(canonical)

        if shortest_length is None or length < shortest_length:
            shortest_length = length
            cycles.clear()

        if length == shortest_length:
            cycles.add(canonical)

    return sorted(cycles)


def _lowest_cost_cycle_slots(cycle_edges, edge_slots, dist):
    """Chooses concrete slots and the cheapest open path around a label cycle."""
    best_cost = float("inf")
    best_path = None
    cycle_length = len(cycle_edges)

    # Resolving a k-cycle needs k-1 swaps, so one ring edge is omitted. Try
    # every possible omission and use dynamic programming for duplicate labels.
    for break_after in range(cycle_length):
        ordered_edges = tuple(
            cycle_edges[(break_after + 1 + offset) % cycle_length]
            for offset in range(cycle_length)
        )
        states = {
            slot_idx: (0.0, (slot_idx,)) for slot_idx in edge_slots[ordered_edges[0]]
        }

        for edge in ordered_edges[1:]:
            next_states = {}

            for slot_idx in edge_slots[edge]:
                choices = [
                    (
                        cost + float(dist[previous_slot, slot_idx]),
                        path + (slot_idx,),
                    )
                    for previous_slot, (cost, path) in states.items()
                ]
                next_states[slot_idx] = min(choices)

            states = next_states

        cost, path = min(states.values())

        if (cost, path) < (best_cost, best_path or ()):
            best_cost = cost
            best_path = path

    return best_cost, best_path


def plan_swaps(slots, current_labels, target_labels, dist):
    """
    Plans all swaps as short mismatch cycles, then minimizes drag distance.

    Correct slots are never disturbed. Short cycles are preferred because each
    independent k-cycle takes only k-1 swaps; reciprocal mismatches therefore
    become one swap instead of being missed by slot-order greedy planning.
    """
    if len(current_labels) != len(target_labels) or dist.shape != (
        len(current_labels),
        len(current_labels),
    ):
        raise ValueError("slot labels and distance matrix must have matching sizes")

    if Counter(current_labels) != Counter(target_labels):
        raise ValueError("current and target labels must contain the same items")

    current = current_labels[:]
    swaps = []

    while current != target_labels:
        edge_slots = {}

        for slot_idx, (current_label, target_label) in enumerate(
            zip(current, target_labels)
        ):
            if current_label != target_label:
                edge_slots.setdefault((current_label, target_label), []).append(
                    slot_idx
                )

        cycles = _shortest_label_cycles(edge_slots)

        if not cycles:
            raise RuntimeError("could not decompose label mismatches into swap cycles")

        _, cycle_slots = min(
            _lowest_cost_cycle_slots(cycle, edge_slots, dist) + (cycle,)
            for cycle in cycles
        )[:2]

        for to_slot, from_slot in zip(cycle_slots, cycle_slots[1:]):
            swaps.append(
                {
                    "from_slot": from_slot,
                    "to_slot": to_slot,
                    "moving_label": current[from_slot],
                    "replaced_label": current[to_slot],
                }
            )
            current[to_slot], current[from_slot] = (
                current[from_slot],
                current[to_slot],
            )

    return swaps


def labels_are_orthogonally_connected(target_labels, adjacency):
    """Checks that every repeated label is connected through four directions."""
    for label in sorted(set(target_labels)):
        indices = {i for i, value in enumerate(target_labels) if value == label}
        pending = [next(iter(indices))]
        visited = set()

        while pending:
            index = pending.pop()

            if index in visited:
                continue

            visited.add(index)
            pending.extend((adjacency[index] & indices) - visited)

        if visited != indices:
            return False

    return True


def layout_compactness_score(slots, target_labels, adjacency):
    """Penalizes straight lines, then rewards orthogonal internal contacts."""
    line_groups = 0
    internal_contacts = 0
    median_width = float(np.median([slot.w for slot in slots]))
    median_height = float(np.median([slot.h for slot in slots]))
    x_tolerance = max(4.0, median_width * ORTHOGONAL_AXIS_TOL_FACTOR)
    y_tolerance = max(4.0, median_height * ORTHOGONAL_AXIS_TOL_FACTOR)

    for label in set(target_labels):
        indices = [i for i, value in enumerate(target_labels) if value == label]
        index_set = set(indices)

        if len(indices) >= 3:
            xs = [slots[index].center[0] for index in indices]
            ys = [slots[index].center[1] for index in indices]

            if max(xs) - min(xs) <= x_tolerance or max(ys) - min(ys) <= y_tolerance:
                line_groups += 1

        internal_contacts += sum(
            len(adjacency[index] & index_set) for index in indices
        ) // 2

    return line_groups, -internal_contacts


def improve_target_compactness(slots, target_labels, adjacency):
    """Locally exchanges boundary cells to remove lines without disconnecting groups."""
    target = list(target_labels)
    current_score = layout_compactness_score(slots, target, adjacency)
    median_width = float(np.median([slot.w for slot in slots]))
    median_height = float(np.median([slot.h for slot in slots]))
    x_tolerance = max(4.0, median_width * ORTHOGONAL_AXIS_TOL_FACTOR)
    y_tolerance = max(4.0, median_height * ORTHOGONAL_AXIS_TOL_FACTOR)

    for _ in range(len(target)):
        best_move = None
        best_cycle = None
        best_score = current_score
        groups = {
            label: {index for index, value in enumerate(target) if value == label}
            for label in sorted(set(target))
        }
        line_labels = set()

        for label, group in groups.items():
            if len(group) < 3:
                continue

            xs = [slots[index].center[0] for index in group]
            ys = [slots[index].center[1] for index in group]

            if max(xs) - min(xs) <= x_tolerance or max(ys) - min(ys) <= y_tolerance:
                line_labels.add(label)

        for label, group in groups.items():
            frontier = set().union(*(adjacency[index] for index in group)) - group

            for source in group:
                for destination in frontier:
                    other_label = target[destination]

                    if other_label == label:
                        continue

                    target[source], target[destination] = (
                        target[destination],
                        target[source],
                    )

                    if labels_are_orthogonally_connected(target, adjacency):
                        score = layout_compactness_score(slots, target, adjacency)

                        if score < best_score:
                            best_score = score
                            best_move = ((source,), (destination,))

                    target[source], target[destination] = (
                        target[destination],
                        target[source],
                    )

        checked_label_pairs = set()

        for label in sorted(line_labels):
            group = groups[label]
            frontier_labels = {
                target[index]
                for source in group
                for index in adjacency[source]
                if target[index] != label
            }

            for other_label in sorted(frontier_labels):
                pair_key = frozenset((label, other_label))

                if pair_key in checked_label_pairs:
                    continue

                checked_label_pairs.add(pair_key)
                other_group = groups[other_label]

                for amount in range(2, min(3, len(group), len(other_group)) + 1):
                    for sources in itertools.combinations(sorted(group), amount):
                        for destinations in itertools.combinations(
                            sorted(other_group),
                            amount,
                        ):
                            for source, destination in zip(sources, destinations):
                                target[source], target[destination] = (
                                    target[destination],
                                    target[source],
                                )

                            if labels_are_orthogonally_connected(target, adjacency):
                                score = layout_compactness_score(
                                    slots,
                                    target,
                                    adjacency,
                                )

                                if score < best_score:
                                    best_score = score
                                    best_move = (sources, destinations)

                            for source, destination in zip(sources, destinations):
                                target[source], target[destination] = (
                                    target[destination],
                                    target[source],
                                )

        if best_move is None and line_labels:
            for label in sorted(line_labels):
                group = groups[label]
                frontier = set().union(
                    *(adjacency[index] for index in group)
                ) - group

                for source in sorted(group):
                    for destination in sorted(frontier):
                        for third in range(len(target)):
                            labels = (
                                target[source],
                                target[destination],
                                target[third],
                            )

                            if len(set(labels)) < 3:
                                continue

                            for rotated in (
                                (labels[2], labels[0], labels[1]),
                                (labels[1], labels[2], labels[0]),
                            ):
                                target[source], target[destination], target[third] = (
                                    rotated
                                )

                                if labels_are_orthogonally_connected(
                                    target,
                                    adjacency,
                                ):
                                    score = layout_compactness_score(
                                        slots,
                                        target,
                                        adjacency,
                                    )

                                    if score < best_score:
                                        best_score = score
                                        best_cycle = (
                                            (source, destination, third),
                                            rotated,
                                        )

                                target[source], target[destination], target[third] = (
                                    labels
                                )

        if best_move is None and best_cycle is None:
            break

        if best_move is not None:
            sources, destinations = best_move

            for source, destination in zip(sources, destinations):
                target[source], target[destination] = (
                    target[destination],
                    target[source],
                )
        else:
            indices, values = best_cycle

            for index, value in zip(indices, values):
                target[index] = value

        current_score = best_score

    return target


def optimize_orthogonal_plan(slots):
    """Finds the fewest-swap target among valid cardinally connected layouts."""
    current_labels = [slot.label for slot in slots]
    dist = pairwise_distance_matrix(slots)
    adjacency = build_orthogonal_adjacency(slots)
    scan_orders = orthogonal_scan_orders(slots, adjacency)
    best = None
    finalists = []
    seen_targets = set()

    def evaluate(target_labels):
        nonlocal best
        target_key = tuple(target_labels)

        if target_key in seen_targets:
            return
        if not labels_are_orthogonally_connected(target_labels, adjacency):
            return

        seen_targets.add(target_key)
        swaps = plan_swaps(
            slots=slots,
            current_labels=current_labels,
            target_labels=target_labels,
            dist=dist,
        )
        drag_distance = sum(
            float(dist[swap["from_slot"], swap["to_slot"]]) for swap in swaps
        )
        score = (
            *layout_compactness_score(slots, target_labels, adjacency),
            len(swaps),
            drag_distance,
            target_key,
        )

        if best is None or score < best[0]:
            best = (score, target_labels, swaps)

        finalists.append((score, target_labels, swaps))
        finalists.sort(key=lambda candidate: candidate[0])
        del finalists[COMPACTNESS_FINALISTS:]

    for scan_order in scan_orders:
        for label_order in candidate_label_orders(current_labels):
            evaluate(
                target_labels_for_scan(
                    current_labels,
                    scan_order,
                    label_order,
                )
            )

    rng = random.Random(LABEL_ORDER_SEED)

    for scan_order in scan_orders:
        for target_labels in candidate_targets_for_scan(
            current_labels,
            scan_order,
            adjacency,
            rng,
        ):
            evaluate(target_labels)

    if best is None:
        for _ in range(CONNECTED_REGION_TRIALS):
            target_labels = grow_connected_target(current_labels, adjacency, rng)

            if target_labels is not None:
                evaluate(target_labels)

            target_labels = peel_connected_target(current_labels, adjacency, rng)

            if target_labels is not None:
                evaluate(target_labels)

    if best is None:
        raise RuntimeError(
            "could not allocate connected top/right/bottom/left item regions"
        )

    improved_finalists = []

    for _, candidate_target, candidate_swaps in finalists:
        improved_target = improve_target_compactness(
            slots,
            candidate_target,
            adjacency,
        )

        if improved_target != candidate_target:
            candidate_swaps = plan_swaps(
                slots=slots,
                current_labels=current_labels,
                target_labels=improved_target,
                dist=dist,
            )

        drag_distance = sum(
            float(dist[swap["from_slot"], swap["to_slot"]])
            for swap in candidate_swaps
        )
        score = (
            *layout_compactness_score(slots, improved_target, adjacency),
            len(candidate_swaps),
            drag_distance,
            tuple(improved_target),
        )
        improved_finalists.append((score, improved_target, candidate_swaps))

    _, target_labels, swaps = min(
        improved_finalists,
        key=lambda candidate: candidate[0],
    )

    if not labels_are_orthogonally_connected(target_labels, adjacency):
        raise RuntimeError("compactness optimization disconnected an item group")

    return target_labels, swaps, adjacency


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
    for k, swap in enumerate(swaps, start=1):
        src_slot = swap["from_slot"]
        dst_slot = swap["to_slot"]

        src_xy = slots[src_slot].screen_center
        dst_xy = slots[dst_slot].screen_center

        print(
            f"Swap {k}: "
            f"{swap['moving_label']} from slot {src_slot} "
            f"to slot {dst_slot}, replacing {swap['replaced_label']}"
        )

        drag_swap(src_xy, dst_xy)
        time.sleep(AFTER_SWAP_DELAY)


# ---------------- main ----------------


def main(detect_only=False, dry_run=False):
    time.sleep(1)

    screenshot = pyautogui.screenshot()
    screenshot_img = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

    diagnostics = {}
    detections = detect_all_items(screenshot_img, diagnostics=diagnostics)
    raw_debug_path, annotated_debug_path, scores_debug_path = (
        save_detection_debug_images(
            screenshot_img,
            detections,
            diagnostics=diagnostics,
        )
    )

    print(f"Detected {len(detections)} items.")
    print(
        "Detection debug files: "
        f"{raw_debug_path}, {annotated_debug_path}, {scores_debug_path}"
    )

    if detect_only:
        print("Detection-only mode. No mouse actions were executed.")
        return

    if not detections:
        print("No items detected. Try lowering THRESHOLD.")
        return

    all_slots = stable_sort_slots(detections)
    slots = largest_orthogonal_component(all_slots)
    excluded_count = len(all_slots) - len(slots)

    if excluded_count:
        print(
            f"Excluded {excluded_count} detections outside the main "
            "orthogonal item grid."
        )

    target_labels, swaps, _ = optimize_orthogonal_plan(slots)

    print(f"Planned {len(swaps)} swaps.")
    print(f"Target label order: {target_labels}")

    print("\nSwap plan:")
    for i, swap in enumerate(swaps, start=1):
        print(
            f"{i}. slot {swap['from_slot']} -> slot {swap['to_slot']} | "
            f"{swap['moving_label']} swaps with {swap['replaced_label']}"
        )

    if DRY_RUN or dry_run:
        print("\nDry-run mode. No mouse actions were executed.")
        return

    execute_swaps(slots, swaps)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="capture and annotate detections without planning or executing swaps",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="capture, detect, and print the swap plan without moving the mouse",
    )
    args = parser.parse_args()
    main(detect_only=args.detect_only, dry_run=args.dry_run)
