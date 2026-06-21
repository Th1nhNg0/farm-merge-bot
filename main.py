import csv
import itertools
import random
import time
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
    "cuu"
]

levels = [1, 2, 3]
item_levels = {"go": [1, 2, 3, 4, 5], "da": [1, 2, 3, 4, 5], "congcu": [1, 2, 3, 4, 5]}

# Detection settings
THRESHOLD = 0.70
TEMPLATE_DIR = Path("images")
TEMPLATE_SCALES = (1.00,)
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

# The game is a large, densely saturated rectangle. Discord panels and
# terminals are smaller or much less dense.
GAME_MIN_SATURATION = 30
GAME_MIN_BRIGHTNESS = 30
GAME_MIN_SCREEN_AREA = 0.10
GAME_MIN_FILL_RATIO = 0.50
GAME_CROP_PADDING = 2

# Isometric layout settings
# Logical top/right/bottom/left neighbors in an isometric board appear as
# diagonal screen neighbors. Screen-horizontal and screen-vertical neighbors
# are logical diagonals and are deliberately excluded from adjacency.
GRID_ANCHOR_Y_FACTOR = 0.72
ISOMETRIC_AXIS_TOLERANCE = 0.65
ISOMETRIC_MIN_STEP_FACTOR = 0.45
ISOMETRIC_MAX_STEP_FACTOR = 1.70
EXACT_LABEL_ORDER_LIMIT = 6
LABEL_ORDER_TRIALS = 96
LABEL_ORDER_SEED = 20260619
CONNECTED_REGION_TRIALS = 8
PLAN_SHORTLIST_SIZE = 24
TARGET_REPAIR_BEAM_WIDTH = 8
TARGET_REPAIR_DEPTH = 8
TARGET_REPAIR_EXACT_LIMIT = 40

# Swap settings
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

    @property
    def grid_anchor(self):
        """Approximate tile anchor used only for isometric geometry."""
        return self.x + self.w // 2, self.y + int(round(self.h * GRID_ANCHOR_Y_FACTOR))


@dataclass(frozen=True)
class PreparedTemplate:
    """Template variant cached in all expensive preprocessing forms."""

    label: str
    item: str
    level: int
    template_name: str
    w: int
    h: int
    features: tuple


_PREPARED_TEMPLATE_CACHE = {}


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


def combined_match_score(screenshot_features, template_features):
    """Scores a cached template against a screenshot feature pair."""
    template_gray, template_edges = template_features
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


def scaled_templates(template, template_scales=None):
    """Yields unique configured template sizes, including the original size."""
    if template_scales is None:
        template_scales = TEMPLATE_SCALES
    original_h, original_w = template.shape[:2]
    seen_sizes = set()

    for scale in template_scales:
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


def configured_labels():
    """Yields every configured item-level label in detection order."""
    for item in items:
        for level in item_levels.get(item, levels):
            yield item, level, f"{item}_{level}"


def load_prepared_templates(template_scales=None):
    """Reads and preprocesses templates once per process and scale tuple."""
    if template_scales is None:
        template_scales = TEMPLATE_SCALES

    template_scales = tuple(template_scales)
    labels = tuple(configured_labels())
    template_state = tuple(
        (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
        for item, level, _ in labels
        for path in template_paths(item, level)
    )
    cache_key = (template_scales, labels, template_state)
    cached = _PREPARED_TEMPLATE_CACHE.get(cache_key)

    if cached is not None:
        return cached

    prepared = []
    missing = []

    for item, level, label in labels:
        paths = template_paths(item, level)

        if not paths:
            missing.append((item, level, label))
            continue

        for template_path in paths:
            template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)

            if template is None:
                print(f"Skipping unreadable template: {template_path}")
                continue

            for scaled_template in scaled_templates(template, template_scales):
                th, tw = scaled_template.shape[:2]
                prepared.append(
                    PreparedTemplate(
                        label=label,
                        item=item,
                        level=level,
                        template_name=template_path.name,
                        w=int(tw),
                        h=int(th),
                        features=matching_features(scaled_template),
                    )
                )

    _PREPARED_TEMPLATE_CACHE[cache_key] = (prepared, missing)
    return prepared, missing


def _init_diagnostics(threshold):
    return {
        "best_score": float("-inf"),
        "best_template": "",
        "best_width": 0,
        "best_height": 0,
        "best_x": 0,
        "best_y": 0,
        "threshold": threshold,
        "detected_count": 0,
    }


def detect_all_items(
    screenshot_img,
    diagnostics=None,
    template_scales=None,
    offset=(0, 0),
):
    screenshot_features = matching_features(screenshot_img)
    screenshot_h, screenshot_w = screenshot_img.shape[:2]
    detections = []
    offset_x, offset_y = offset
    prepared_templates, missing_templates = load_prepared_templates(template_scales)

    for item, level, _ in missing_templates:
        print(
            "Skipping missing templates: "
            f"{TEMPLATE_DIR / f'{item}{level}.png'} or "
            f"{TEMPLATE_DIR / f'{item}{level}_<variant>.png'}"
        )

    if diagnostics is not None:
        for _, _, label in configured_labels():
            diagnostics[label] = _init_diagnostics(
                TEMPLATE_THRESHOLDS.get(label, THRESHOLD)
            )

    for template in prepared_templates:
        if template.h > screenshot_h or template.w > screenshot_w:
            continue

        label = template.label
        threshold = TEMPLATE_THRESHOLDS.get(label, THRESHOLD)
        result = combined_match_score(screenshot_features, template.features)

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
                        "best_template": template.template_name,
                        "best_width": template.w,
                        "best_height": template.h,
                        "best_x": int(best_x + offset_x),
                        "best_y": int(best_y + offset_y),
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
                    item=template.item,
                    level=template.level,
                    x=int(x + offset_x),
                    y=int(y + offset_y),
                    w=template.w,
                    h=template.h,
                    score=float(result[y, x]),
                )
            )

    detections = deduplicate_detections(detections)

    if diagnostics is not None:
        detected_counts = Counter(detection.label for detection in detections)

        for label, values in diagnostics.items():
            values["detected_count"] = detected_counts[label]

    return detections


def save_detection_debug_images(
    screenshot_img, detections, diagnostics=None, image_offset=(0, 0)
):
    """Saves the captured board and an annotated copy for calibration."""
    DETECTION_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = DETECTION_DEBUG_DIR / "board.png"
    annotated_path = DETECTION_DEBUG_DIR / "detections.png"
    scores_path = DETECTION_DEBUG_DIR / "scores.csv"
    annotated = screenshot_img.copy()
    offset_x, offset_y = image_offset
    for detection in detections:
        draw_x = detection.x - offset_x
        draw_y = detection.y - offset_y
        top_left = (draw_x, draw_y)
        bottom_right = (draw_x + detection.w, draw_y + detection.h)
        cv2.rectangle(annotated, top_left, bottom_right, (0, 255, 0), 1)
        cv2.putText(
            annotated,
            f"{detection.label} {detection.score:.2f}",
            (draw_x, max(10, draw_y - 3)),
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
    centers = np.array([d.screen_center for d in slots], dtype=np.float32)
    diff = centers[:, None, :] - centers[None, :, :]
    return np.sqrt(np.sum(diff * diff, axis=2))


def layout_points(slots):
    """Returns the points used to infer board geometry, not drag positions."""
    return np.array([slot.grid_anchor for slot in slots], dtype=np.float32)


def stable_sort_slots(detections):
    """Gives stable slot indices from top-to-bottom, left-to-right."""
    return sorted(detections, key=lambda d: (d.grid_anchor[1], d.grid_anchor[0]))


def estimate_isometric_step(slots):
    """Infers the cardinal isometric step from detected slot anchors."""
    if len(slots) <= 1:
        return 1.0, 1.0, 1.0

    points = layout_points(slots)
    diff = points[:, None, :] - points[None, :, :]
    dx = diff[:, :, 0]
    dy = diff[:, :, 1]
    distances = np.sqrt(dx * dx + dy * dy)
    np.fill_diagonal(distances, np.inf)

    nearest = np.min(distances, axis=1)
    finite_nearest = nearest[np.isfinite(nearest)]
    median_nearest = float(np.median(finite_nearest)) if len(finite_nearest) else 1.0

    median_width = float(np.median([slot.w for slot in slots]))
    median_height = float(np.median([slot.h for slot in slots]))
    min_dx = max(3.0, median_width * 0.12)
    min_dy = max(3.0, median_height * 0.08)

    candidate_mask = (
        (distances <= median_nearest * 1.80)
        & (np.abs(dx) >= min_dx)
        & (np.abs(dy) >= min_dy)
    )

    candidate_dx = np.abs(dx[candidate_mask])
    candidate_dy = np.abs(dy[candidate_mask])

    if len(candidate_dx) and len(candidate_dy):
        step_x = float(np.median(candidate_dx))
        step_y = float(np.median(candidate_dy))
    else:
        # Conservative fallback for a common 2:1 isometric projection.
        step_x = max(1.0, median_width * 0.50)
        step_y = max(1.0, median_height * 0.35)

    step_distance = float(np.hypot(step_x, step_y))
    return step_x, step_y, max(step_distance, 1.0)


def build_isometric_adjacency(slots):
    """
    Connects logical top/right/bottom/left neighbors on an isometric board.

    In screen coordinates, valid cardinal neighbors are the four diagonal
    directions: up-right, down-right, down-left, and up-left. This function
    therefore does not connect screen-horizontal or screen-vertical neighbors,
    because those are diagonal neighbors in the underlying isometric grid.
    """
    adjacency = {index: set() for index in range(len(slots))}

    if len(slots) <= 1:
        return adjacency

    step_x, step_y, step_distance = estimate_isometric_step(slots)
    points = layout_points(slots)

    for index, point in enumerate(points):
        directions = {
            "up_right": [],
            "down_right": [],
            "down_left": [],
            "up_left": [],
        }

        for other_index, other_point in enumerate(points):
            if index == other_index:
                continue

            dx = float(other_point[0] - point[0])
            dy = float(other_point[1] - point[1])

            # Screen-horizontal / screen-vertical relations are logical
            # diagonals on an isometric grid, so they are intentionally ignored.
            if abs(dx) < step_x * ISOMETRIC_MIN_STEP_FACTOR:
                continue
            if abs(dy) < step_y * ISOMETRIC_MIN_STEP_FACTOR:
                continue

            norm_dx = abs(dx) / step_x
            norm_dy = abs(dy) / step_y
            normalized_step = max(norm_dx, norm_dy)
            axis_error = abs(norm_dx - norm_dy)

            if normalized_step > ISOMETRIC_MAX_STEP_FACTOR:
                continue
            if axis_error > ISOMETRIC_AXIS_TOLERANCE:
                continue

            distance = float(np.hypot(dx, dy))
            if distance > step_distance * ISOMETRIC_MAX_STEP_FACTOR:
                continue

            if dx > 0 and dy < 0:
                direction = "up_right"
            elif dx > 0 and dy > 0:
                direction = "down_right"
            elif dx < 0 and dy > 0:
                direction = "down_left"
            else:
                direction = "up_left"

            # Prefer vectors closest to one inferred isometric step.
            cost = axis_error + abs(norm_dx - 1.0) + abs(norm_dy - 1.0)
            directions[direction].append((cost, distance, other_index))

        for candidates in directions.values():
            if not candidates:
                continue

            _, _, neighbor = min(candidates)
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
    """Removes isolated full-screen matches outside the main isometric item grid."""
    if not slots:
        return []

    adjacency = build_isometric_adjacency(slots)
    component = connected_components(adjacency)[0]
    return [slot for index, slot in enumerate(slots) if index in component]


def _numeric_axis_groups(values, tolerance):
    """Groups numeric coordinates into bands."""
    groups = []

    for index in sorted(range(len(values)), key=lambda i: values[i]):
        coordinate = values[index]

        if not groups:
            groups.append([index])
            continue

        group_coordinate = float(np.median([values[i] for i in groups[-1]]))

        if abs(coordinate - group_coordinate) <= tolerance:
            groups[-1].append(index)
        else:
            groups.append([index])

    return groups


def _screen_snake_orders(slots):
    points = layout_points(slots)
    orders = [
        tuple(sorted(range(len(slots)), key=lambda i: (points[i, 1], points[i, 0]))),
        tuple(sorted(range(len(slots)), key=lambda i: (points[i, 1], -points[i, 0]))),
        tuple(sorted(range(len(slots)), key=lambda i: (points[i, 0], points[i, 1]))),
        tuple(sorted(range(len(slots)), key=lambda i: (points[i, 0], -points[i, 1]))),
    ]
    return orders + [tuple(reversed(order)) for order in orders]


def orthogonal_scan_orders(slots):
    """Returns isometric row/column snake orders for target generation."""
    if not slots:
        return [()]

    step_x, step_y, _ = estimate_isometric_step(slots)
    points = layout_points(slots)
    xs = points[:, 0]
    ys = points[:, 1]

    # Convert screen coordinates to approximate isometric grid axes. Neighbors
    # along a cardinal isometric direction change exactly one of these axes.
    iso_u = 0.5 * ((ys / step_y) + (xs / step_x))
    iso_v = 0.5 * ((ys / step_y) - (xs / step_x))
    candidates = []

    for primary_values, secondary_values in ((iso_u, iso_v), (iso_v, iso_u)):
        groups = _numeric_axis_groups(primary_values, tolerance=0.55)

        for reverse_groups in (False, True):
            ordered_groups = list(reversed(groups)) if reverse_groups else groups

            for reverse_first_group in (False, True):
                order = []

                for group_index, group in enumerate(ordered_groups):
                    reverse = reverse_first_group != (group_index % 2 == 1)
                    ordered = sorted(
                        group,
                        key=lambda i: secondary_values[i],
                        reverse=reverse,
                    )
                    order.extend(ordered)

                candidates.append(tuple(order))

    candidates.extend(_screen_snake_orders(slots))
    return list(dict.fromkeys(candidates))


def candidate_label_orders(current_labels):
    """Yields exhaustive small-board orders and deterministic large-board trials."""
    labels = tuple(sorted(set(current_labels)))

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
    """Places each label in one connected segment of a scan path."""
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
                right in adjacency[left] for left, right in zip(segment, segment[1:])
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
                available_labels = sorted(
                    label for label, count in label_counts.items() if count == size
                )

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
        rotations = [cycle_edges[i:] + cycle_edges[:i] for i in range(len(cycle_edges))]
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


def plan_swaps(current_labels, target_labels, dist):
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


def labels_are_cardinally_connected(target_labels, adjacency):
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
    """Penalizes straight isometric lines, then rewards cardinal contacts."""
    step_x, step_y, _ = estimate_isometric_step(slots)
    points = layout_points(slots)
    iso_u = 0.5 * ((points[:, 1] / step_y) + (points[:, 0] / step_x))
    iso_v = 0.5 * ((points[:, 1] / step_y) - (points[:, 0] / step_x))
    line_groups = 0
    internal_contacts = 0

    for label in sorted(set(target_labels)):
        indices = [i for i, value in enumerate(target_labels) if value == label]
        index_set = set(indices)

        if len(indices) >= 3 and (
            float(np.ptp(iso_u[indices])) <= 0.55
            or float(np.ptp(iso_v[indices])) <= 0.55
        ):
            line_groups += 1

        internal_contacts += (
            sum(len(adjacency[index] & index_set) for index in indices) // 2
        )

    # Lower scores are better. Lines are rejected before contact maximization.
    return line_groups, -internal_contacts


def refine_target_assignments(
    slots,
    current_labels,
    target_labels,
    adjacency,
    dist,
    swaps,
):
    """Moves target labels closer to their current slots without quality loss."""
    required_quality = layout_compactness_score(slots, target_labels, adjacency)
    working_target = list(target_labels)
    best_target = working_target
    best_swaps = swaps
    best_score = (
        len(swaps),
        sum(float(dist[swap["from_slot"], swap["to_slot"]]) for swap in swaps),
    )

    if not swaps:
        return best_target, best_swaps

    while True:
        mismatch_count = sum(
            current != target
            for current, target in zip(current_labels, working_target)
        )
        mismatched_slots = [
            index
            for index, (current, target) in enumerate(
                zip(current_labels, working_target)
            )
            if current != target
        ]
        best_repair = None

        for offset, left in enumerate(mismatched_slots):
            for right in mismatched_slots[offset + 1 :]:
                if working_target[left] == working_target[right]:
                    continue
                if (
                    working_target[right] != current_labels[left]
                    and working_target[left] != current_labels[right]
                ):
                    continue

                candidate = working_target[:]
                candidate[left], candidate[right] = candidate[right], candidate[left]
                candidate_mismatches = sum(
                    current != target
                    for current, target in zip(current_labels, candidate)
                )

                if candidate_mismatches >= mismatch_count:
                    continue
                if (
                    layout_compactness_score(slots, candidate, adjacency)
                    != required_quality
                ):
                    continue
                if not labels_are_cardinally_connected(candidate, adjacency):
                    continue

                repair_key = (candidate_mismatches, left, right)

                if best_repair is None or repair_key < best_repair[0]:
                    best_repair = (repair_key, candidate)

        if best_repair is None:
            break

        working_target = best_repair[1]
        candidate_swaps = plan_swaps(current_labels, working_target, dist)
        candidate_score = (
            len(candidate_swaps),
            sum(
                float(dist[swap["from_slot"], swap["to_slot"]])
                for swap in candidate_swaps
            ),
        )

        if candidate_score < best_score:
            best_target = working_target
            best_swaps = candidate_swaps
            best_score = candidate_score

    step_x, step_y, _ = estimate_isometric_step(slots)
    points = layout_points(slots)
    iso_u = 0.5 * ((points[:, 1] / step_y) + (points[:, 0] / step_x))
    iso_v = 0.5 * ((points[:, 1] / step_y) - (points[:, 0] / step_x))

    def label_score(labels, label):
        indices = [index for index, value in enumerate(labels) if value == label]
        index_set = set(indices)
        line_group = int(
            len(indices) >= 3
            and (
                float(np.ptp(iso_u[indices])) <= 0.55
                or float(np.ptp(iso_v[indices])) <= 0.55
            )
        )
        contacts = (
            sum(len(adjacency[index] & index_set) for index in indices) // 2
        )
        return line_group, -contacts

    def label_is_connected(labels, label):
        indices = {index for index, value in enumerate(labels) if value == label}
        pending = [next(iter(indices))]
        visited = set()

        while pending:
            index = pending.pop()

            if index in visited:
                continue

            visited.add(index)
            pending.extend((adjacency[index] & indices) - visited)

        return visited == indices

    beam = [best_target]
    visited_targets = {tuple(best_target)}
    best_rank = (*best_score, tuple(best_target))

    for _ in range(TARGET_REPAIR_DEPTH):
        generated = []

        for state in beam:
            base_mismatches = sum(
                current != target
                for current, target in zip(current_labels, state)
            )
            contributions = {
                label: label_score(state, label) for label in set(state)
            }

            for left in range(len(state)):
                for right in range(left + 1, len(state)):
                    if state[left] == state[right]:
                        continue

                    candidate = state[:]
                    candidate[left], candidate[right] = (
                        candidate[right],
                        candidate[left],
                    )
                    candidate_key = tuple(candidate)

                    if candidate_key in visited_targets:
                        continue

                    candidate_mismatches = sum(
                        current != target
                        for current, target in zip(current_labels, candidate)
                    )

                    if candidate_mismatches > base_mismatches:
                        continue

                    left_label = state[left]
                    right_label = state[right]
                    previous_score = tuple(
                        sum(values)
                        for values in zip(
                            contributions[left_label],
                            contributions[right_label],
                        )
                    )
                    candidate_score = tuple(
                        sum(values)
                        for values in zip(
                            label_score(candidate, left_label),
                            label_score(candidate, right_label),
                        )
                    )

                    if candidate_score != previous_score:
                        continue
                    if not label_is_connected(candidate, left_label):
                        continue
                    if not label_is_connected(candidate, right_label):
                        continue

                    visited_targets.add(candidate_key)
                    generated.append(
                        (candidate_mismatches, candidate_key, candidate)
                    )

        generated.sort()
        ranked = []

        for _, candidate_key, candidate in generated[
            :TARGET_REPAIR_EXACT_LIMIT
        ]:
            candidate_swaps = plan_swaps(current_labels, candidate, dist)
            drag_distance = sum(
                float(dist[swap["from_slot"], swap["to_slot"]])
                for swap in candidate_swaps
            )
            candidate_rank = (
                len(candidate_swaps),
                drag_distance,
                candidate_key,
            )
            ranked.append(
                (candidate_rank, candidate, candidate_swaps)
            )

            if candidate_rank < best_rank:
                best_rank = candidate_rank
                best_target = candidate
                best_swaps = candidate_swaps

        ranked.sort(key=lambda candidate: candidate[0])
        beam = [
            candidate
            for _, candidate, _ in ranked[:TARGET_REPAIR_BEAM_WIDTH]
        ]

        if not beam or best_rank[0] == 0:
            break

    return best_target, best_swaps


def optimize_isometric_plan(slots):
    """Ranks connected layouts cheaply, then plans swaps for a small shortlist."""
    current_labels = [slot.label for slot in slots]
    dist = pairwise_distance_matrix(slots)
    adjacency = build_isometric_adjacency(slots)
    scan_orders = orthogonal_scan_orders(slots)
    candidates = {}

    def add_candidate(target_labels):
        target_key = tuple(target_labels)

        if target_key in candidates:
            return
        if not labels_are_cardinally_connected(target_labels, adjacency):
            return

        cheap_score = (
            *layout_compactness_score(slots, target_labels, adjacency),
            sum(
                current != target
                for current, target in zip(current_labels, target_labels)
            ),
            target_key,
        )
        candidates[target_key] = (cheap_score, list(target_labels))

    # This makes a completed board an explicit zero-swap candidate on later runs.
    add_candidate(current_labels)

    for scan_order in scan_orders:
        for label_order in candidate_label_orders(current_labels):
            add_candidate(
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
            add_candidate(target_labels)

    if not candidates:
        for _ in range(CONNECTED_REGION_TRIALS):
            target_labels = grow_connected_target(current_labels, adjacency, rng)

            if target_labels is not None:
                add_candidate(target_labels)

    if not candidates:
        raise RuntimeError(
            "could not allocate connected isometric top/right/bottom/left item regions"
        )

    best_compactness = min(candidate[0][:2] for candidate in candidates.values())
    effective_candidates = [
        candidate
        for candidate in candidates.values()
        if candidate[0][:2] == best_compactness
    ]
    ordered_candidates = sorted(
        effective_candidates,
        key=lambda candidate: (candidate[0][2], candidate[0][3]),
    )
    best_plan = None

    for candidate_index, (cheap_score, target_labels) in enumerate(
        ordered_candidates
    ):
        mismatch_count = cheap_score[2]
        swap_lower_bound = (mismatch_count + 1) // 2

        if best_plan is not None and best_plan[0][0] == 0:
            break

        # Every swap fixes at most two mismatched slots. After the normal
        # shortlist budget, stop once later candidates cannot tie the best
        # exact plan found so far. This retains bounded work in the common case
        # while no longer hiding a potentially better plan behind a hard cut.
        if (
            candidate_index >= PLAN_SHORTLIST_SIZE
            and best_plan is not None
            and swap_lower_bound > best_plan[0][0]
        ):
            break

        swaps = plan_swaps(current_labels, target_labels, dist)
        drag_distance = sum(
            float(dist[swap["from_slot"], swap["to_slot"]]) for swap in swaps
        )
        score = (
            len(swaps),
            drag_distance,
            tuple(target_labels),
        )
        candidate_plan = (score, target_labels, swaps)

        if best_plan is None or candidate_plan < best_plan:
            best_plan = candidate_plan

    _, target_labels, swaps = best_plan
    target_labels, swaps = refine_target_assignments(
        slots,
        current_labels,
        target_labels,
        adjacency,
        dist,
        swaps,
    )

    return target_labels, swaps, adjacency


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


# ---------------- mouse actions ----------------


def drag_swap(src_xy, dst_xy):
    sx, sy = src_xy
    dx, dy = dst_xy

    # The cursor is not interacting with the game yet, so the automatic pause
    # after this positioning move is redundant. Keep all pauses once the drag
    # begins, where they protect input reliability.
    pyautogui.moveTo(sx, sy, duration=0, _pause=False)
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


# ---------------- runtime helpers ----------------


def find_game_region(screenshot_img):
    """Returns the largest dense, colorful viewport as an (x, y, w, h) box."""
    screen_h, screen_w = screenshot_img.shape[:2]
    hsv = cv2.cvtColor(screenshot_img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        (0, GAME_MIN_SATURATION, GAME_MIN_BRIGHTNESS),
        (179, 255, 255),
    )
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    min_area = screen_h * screen_w * GAME_MIN_SCREEN_AREA
    candidates = []

    for x, y, w, h, area in stats[1:]:
        if area >= min_area and area / (w * h) >= GAME_MIN_FILL_RATIO:
            candidates.append((int(area), int(x), int(y), int(w), int(h)))

    if not candidates:
        return None

    _, x, y, w, h = max(candidates)
    left = max(0, x - GAME_CROP_PADDING)
    top = max(0, y - GAME_CROP_PADDING)
    right = min(screen_w, x + w + GAME_CROP_PADDING)
    bottom = min(screen_h, y + h + GAME_CROP_PADDING)
    return left, top, right - left, bottom - top


def capture_game_bgr():
    """Captures the desktop once, then keeps only the detected game viewport."""
    screenshot = pyautogui.screenshot()
    full_image = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
    region = find_game_region(full_image)

    if region is None:
        raise RuntimeError("Game viewport not found. Keep the game visible and retry.")

    x, y, w, h = region
    return full_image[y : y + h, x : x + w], (x, y)


# ---------------- main ----------------


def main():
    screenshot_img, offset = capture_game_bgr()
    print(
        f"Game region: x={offset[0]}, y={offset[1]}, "
        f"w={screenshot_img.shape[1]}, h={screenshot_img.shape[0]}"
    )

    diagnostics = {}
    detections = detect_all_items(
        screenshot_img,
        diagnostics=diagnostics,
        offset=offset,
    )
    raw_path, annotated_path, scores_path = save_detection_debug_images(
        screenshot_img,
        detections,
        diagnostics=diagnostics,
        image_offset=offset,
    )
    print(
        "Detection debug files: "
        f"{raw_path}, {annotated_path}, {scores_path}"
    )

    print(f"Detected {len(detections)} items.")

    if not detections:
        print("No items detected. Check the game window or lower THRESHOLD.")
        return

    all_slots = stable_sort_slots(detections)
    slots = largest_orthogonal_component(all_slots)
    excluded_count = len(all_slots) - len(slots)

    if excluded_count:
        print(
            f"Excluded {excluded_count} detections outside the main "
            "isometric item grid."
        )

    print("Planning swaps...")
    started = time.perf_counter()
    target_labels, swaps, adjacency = optimize_isometric_plan(slots)
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

    execute_swaps(slots, swaps)


if __name__ == "__main__":
    main()
