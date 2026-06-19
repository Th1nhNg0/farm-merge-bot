import argparse
import csv
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
]

levels = [1, 2, 3]
item_levels = {"go": [1, 2, 3, 4, 5], "da": [1, 2, 3, 4, 5], "congcu": [1, 2, 3, 4, 5]}

# Screenshot region: left, top, width, height
REGION = (510, 200, 850, 600)

SCREEN_X0 = REGION[0]
SCREEN_Y0 = REGION[1]

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

# Merge field inside REGION, measured from debug/board.png. Matching outside
# this polygon finds identical decorative crops, bricks, and tools that cannot
# be swapped.
BOARD_POLYGON = (
    (0, 350),
    (850, 42),
    (850, 600),
    (580, 600),
    (0, 425),
)

# Cluster settings
# A cluster is exactly one label: same item + same level.
# Example clusters: huongduong_1, huongduong_2, bo_3.
#
# "compact" is the default because it makes area-like groups instead of long
# one-cell-wide lines. It allows any visually adjacent nearby slot, then scores
# candidate clusters by compactness.
CLUSTER_CONNECTIVITY = "compact"  # "compact", "orthogonal", or "distance"

# Compact adjacency: connect nearby visual neighbors, then choose blob-shaped
# exact-label clusters with a compactness objective.
COMPACT_ADJ_FACTOR = 1.90
COMPACT_MAX_NEIGHBORS = 8
COMPACT_SEED_TRIALS = 12
COMPACT_CONTACT_REWARD = 2.75

# Maximum number of objects in one same-label cluster.
# If one label appears more than this, it is split into several compact
# clusters with the same target label.
MAX_CLUSTER_SIZE = 4
CLUSTER_ALLOCATION_ORDERS = (
    "top_left",
    "top_right",
    "bottom_left",
    "bottom_right",
    "label_forward",
    "label_reverse",
)

# Orthogonal connectivity means left/right/up/down only.
# It is stricter, but on an isometric board it can easily make vertical or
# horizontal lines. Keep it only for comparison.
ORTHOGONAL_AXIS_TOL_FACTOR = 0.45
ORTHOGONAL_MAX_STEP_FACTOR = 1.60
ORTHOGONAL_RELAXATION_STEPS = [1.0, 1.15, 1.30]

# Distance fallback retained for debugging/comparison.
DISTANCE_CLUSTER_FACTOR = 1.35
DISTANCE_CLUSTER_NEIGHBORS = 4

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
        return (
            SCREEN_X0 + self.x + self.w // 2,
            SCREEN_Y0 + self.y + self.h // 2,
        )


# ---------------- detection ----------------


def template_paths(item, level):
    """Returns the base template and optional variants such as bo1_2.png."""
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


def make_board_mask(image_shape):
    mask = np.zeros(image_shape[:2], dtype=np.uint8)
    polygon = np.array(BOARD_POLYGON, dtype=np.int32)
    cv2.fillPoly(mask, [polygon], 255)
    return mask


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
    board_mask = make_board_mask(screenshot_img.shape)
    detections = []

    for item in items:
        for level in item_levels.get(item, levels):
            paths = template_paths(item, level)

            if not paths:
                print(
                    f"Skipping missing template: {TEMPLATE_DIR / f'{item}{level}.png'}"
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
                    valid_centers = board_mask[
                        th // 2 : th // 2 + result.shape[0],
                        tw // 2 : tw // 2 + result.shape[1],
                    ]
                    result = np.where(valid_centers > 0, result, -1.0)

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
    cv2.polylines(
        annotated,
        [np.array(BOARD_POLYGON, dtype=np.int32)],
        isClosed=True,
        color=(255, 128, 0),
        thickness=2,
    )

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


def save_swap_plan_debug_image(screenshot_img, slots, swaps):
    """Draws numbered drag arrows over the captured board."""
    DETECTION_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DETECTION_DEBUG_DIR / "swap_plan.png"
    annotated = screenshot_img.copy()

    for number, swap in enumerate(swaps, start=1):
        source = tuple(int(value) for value in slots[swap["from_slot"]].center)
        destination = tuple(int(value) for value in slots[swap["to_slot"]].center)
        midpoint = (
            (source[0] + destination[0]) // 2,
            (source[1] + destination[1]) // 2,
        )
        cv2.arrowedLine(
            annotated,
            source,
            destination,
            (255, 0, 255),
            1,
            cv2.LINE_AA,
            tipLength=0.18,
        )
        cv2.putText(
            annotated,
            str(number),
            midpoint,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.34,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            annotated,
            str(number),
            midpoint,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.34,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.imwrite(str(output_path), annotated)
    return output_path


# ---------------- geometry ----------------


def pairwise_distance_matrix(slots):
    centers = np.array([d.center for d in slots], dtype=np.float32)
    diff = centers[:, None, :] - centers[None, :, :]
    return np.sqrt(np.sum(diff * diff, axis=2))


def stable_sort_slots(detections):
    """
    Gives stable slot indices from top-to-bottom, left-to-right.

    The cluster algorithm still uses true 2D geometry; this ordering only makes
    swap planning and console logs deterministic.
    """
    return sorted(detections, key=lambda d: (d.center[1], d.center[0]))


def centroid_of_indices(slots, indices):
    pts = np.array([slots[i].center for i in indices], dtype=np.float32)

    if len(pts) == 0:
        return np.array([0.0, 0.0], dtype=np.float32)

    return np.median(pts, axis=0)


def median_nearest_neighbor_distance(dist):
    n = dist.shape[0]

    if n <= 1:
        return 1.0

    nearest = []

    for i in range(n):
        vals = [float(dist[i, j]) for j in range(n) if j != i]
        nearest.append(min(vals))

    return float(np.median(nearest))


# ---------------- cluster graph ----------------


def orthogonal_edge_type(slots, i, j, axis_tol, max_step):
    xi, yi = slots[i].center
    xj, yj = slots[j].center

    dx = abs(xi - xj)
    dy = abs(yi - yj)

    if dx == 0 and dy == 0:
        return None

    # Same inferred row: left/right edge only.
    if dy <= axis_tol and 0 < dx <= max_step:
        return "horizontal"

    # Same inferred column: up/down edge only.
    if dx <= axis_tol and 0 < dy <= max_step:
        return "vertical"

    return None


def build_orthogonal_adjacency(slots, indices, dist, median_nn, relaxation=1.0):
    """
    Builds a strict four-neighbor graph for cluster allocation.

    A slot connects only to the nearest valid left, right, up, and down
    neighbor. Diagonal-only proximity is not accepted.
    """
    idxs = list(indices)
    adj = {i: set() for i in idxs}

    if len(idxs) <= 1:
        return adj

    median_w = float(np.median([slots[i].w for i in idxs]))
    median_h = float(np.median([slots[i].h for i in idxs]))

    axis_tol = max(8.0, max(median_w, median_h) * ORTHOGONAL_AXIS_TOL_FACTOR)
    axis_tol *= relaxation

    max_step = max(median_nn * ORTHOGONAL_MAX_STEP_FACTOR, max(median_w, median_h))
    max_step *= relaxation

    for i in idxs:
        xi, yi = slots[i].center

        candidates_by_direction = {
            "left": [],
            "right": [],
            "up": [],
            "down": [],
        }

        for j in idxs:
            if i == j:
                continue

            edge_type = orthogonal_edge_type(slots, i, j, axis_tol, max_step)

            if edge_type is None:
                continue

            xj, yj = slots[j].center

            if edge_type == "horizontal":
                direction = "left" if xj < xi else "right"
            else:
                direction = "up" if yj < yi else "down"

            candidates_by_direction[direction].append(j)

        for candidates in candidates_by_direction.values():
            if not candidates:
                continue

            nearest = min(candidates, key=lambda j: float(dist[i, j]))
            adj[i].add(nearest)
            adj[nearest].add(i)

    return adj


def build_distance_adjacency(indices, dist, median_nn, relaxation=1.0):
    """
    Optional non-orthogonal graph, retained for comparison.

    The default code path does not use this unless CLUSTER_CONNECTIVITY is set
    to "distance".
    """
    idxs = list(indices)
    adj = {i: set() for i in idxs}

    if len(idxs) <= 1:
        return adj

    max_edge = median_nn * DISTANCE_CLUSTER_FACTOR * relaxation

    for i in idxs:
        candidates = [j for j in idxs if j != i and dist[i, j] <= max_edge]
        candidates.sort(key=lambda j: float(dist[i, j]))

        if not candidates:
            nearest = min((j for j in idxs if j != i), key=lambda j: float(dist[i, j]))
            candidates = [nearest]

        for j in candidates[:DISTANCE_CLUSTER_NEIGHBORS]:
            adj[i].add(j)
            adj[j].add(i)

    return adj


def build_compact_adjacency(indices, dist, median_nn, relaxation=1.0):
    """
    Builds a visual-neighbor graph for compact blob clustering.

    This is less axis-restricted than orthogonal adjacency. It is usually a
    better model for isometric boards because visually adjacent cells often do
    not share exactly the same x or y coordinate.
    """
    idxs = list(indices)
    adj = {i: set() for i in idxs}

    if len(idxs) <= 1:
        return adj

    max_edge = median_nn * COMPACT_ADJ_FACTOR * relaxation

    for i in idxs:
        candidates = [j for j in idxs if j != i and dist[i, j] <= max_edge]
        candidates.sort(key=lambda j: float(dist[i, j]))

        if not candidates:
            nearest = min((j for j in idxs if j != i), key=lambda j: float(dist[i, j]))
            candidates = [nearest]

        for j in candidates[:COMPACT_MAX_NEIGHBORS]:
            adj[i].add(j)
            adj[j].add(i)

    return adj


def build_cluster_adjacency(slots, indices, dist, median_nn, relaxation=1.0):
    if CLUSTER_CONNECTIVITY == "compact":
        return build_compact_adjacency(
            indices=indices,
            dist=dist,
            median_nn=median_nn,
            relaxation=relaxation,
        )

    if CLUSTER_CONNECTIVITY == "orthogonal":
        return build_orthogonal_adjacency(
            slots=slots,
            indices=indices,
            dist=dist,
            median_nn=median_nn,
            relaxation=relaxation,
        )

    if CLUSTER_CONNECTIVITY == "distance":
        return build_distance_adjacency(
            indices=indices,
            dist=dist,
            median_nn=median_nn,
            relaxation=relaxation,
        )

    raise ValueError(
        "CLUSTER_CONNECTIVITY must be 'compact', 'orthogonal', or 'distance'"
    )


def connected_components(indices, adj):
    remaining = set(indices)
    components = []

    while remaining:
        start = next(iter(remaining))
        stack = [start]
        component = set()

        while stack:
            i = stack.pop()

            if i in component:
                continue

            component.add(i)
            remaining.discard(i)

            for nb in adj.get(i, set()):
                if nb in remaining and nb not in component:
                    stack.append(nb)

        components.append(component)

    return components


# ---------------- label-cluster allocation ----------------


def label_seed_score(
    slots, i, current_label_indices, target_point, adj, dist, median_nn
):
    """
    Scores a possible cluster seed.

    A good seed is close to where the label already exists, has several nearby
    free neighbors, and is already occupied by the same label when possible.
    """
    same_set = set(current_label_indices)
    center = np.array(slots[i].center, dtype=np.float32)

    score = float(np.linalg.norm(center - target_point))

    if i in same_set:
        score -= median_nn * 0.90

    same_nearby = sum(1 for nb in adj.get(i, set()) if nb in same_set)
    degree = len(adj.get(i, set()))

    score -= median_nn * 0.50 * same_nearby
    score -= median_nn * 0.25 * degree

    return score


def choose_seed(
    slots, available, current_label_indices, target_point, adj, dist, median_nn
):
    available = list(available)

    if not available:
        return None

    return min(
        available,
        key=lambda i: label_seed_score(
            slots=slots,
            i=i,
            current_label_indices=current_label_indices,
            target_point=target_point,
            adj=adj,
            dist=dist,
            median_nn=median_nn,
        ),
    )


def choose_seed_candidates(
    slots,
    available,
    current_label_indices,
    target_point,
    adj,
    dist,
    median_nn,
    limit=COMPACT_SEED_TRIALS,
):
    """
    Returns several plausible seeds.

    Trying multiple seeds matters because a single greedy seed can produce a
    thin line even when a better blob-shaped cluster exists nearby.
    """
    ranked = sorted(
        list(available),
        key=lambda i: label_seed_score(
            slots=slots,
            i=i,
            current_label_indices=current_label_indices,
            target_point=target_point,
            adj=adj,
            dist=dist,
            median_nn=median_nn,
        ),
    )

    return ranked[: max(1, min(limit, len(ranked)))]


def choose_best_component(
    slots, components, current_label_indices, target_point, median_nn
):
    same_set = set(current_label_indices)

    best_component = None
    best_key = None

    for component in components:
        component = set(component)
        component_center = centroid_of_indices(slots, list(component))
        d_to_target = float(np.linalg.norm(component_center - target_point))
        same_inside = sum(1 for i in component if i in same_set)

        # First retain as many already-correct items as possible. Geometry is
        # only the tie-breaker, so visual compactness cannot add extra swaps.
        key = (-same_inside, d_to_target)

        if best_key is None or key < best_key:
            best_key = key
            best_component = component

    return best_component


def compactness_score(
    slots, cluster, adj, dist, target_point, current_label_indices, median_nn
):
    """
    Lower is better.

    This penalizes long, line-like clusters by measuring radius and diameter.
    It rewards internal contacts so a 2D blob beats a one-cell-wide chain.
    """
    if not cluster:
        return float("inf")

    cluster = list(cluster)
    cluster_set = set(cluster)
    pts = np.array([slots[i].center for i in cluster], dtype=np.float32)

    centroid = np.median(pts, axis=0)
    target_distance = float(np.linalg.norm(centroid - target_point))

    radius = float(np.mean(np.linalg.norm(pts - centroid, axis=1)))

    if len(cluster) > 1:
        sub = dist[np.ix_(cluster, cluster)]
        diameter = float(np.max(sub))
    else:
        diameter = 0.0

    internal_edges = 0
    for i in cluster:
        internal_edges += sum(1 for nb in adj.get(i, set()) if nb in cluster_set)
    internal_edges //= 2

    same_set = set(current_label_indices)
    same_inside = sum(1 for i in cluster if i in same_set)

    return (
        0.40 * target_distance
        + 0.35 * radius
        + 0.25 * diameter
        - COMPACT_CONTACT_REWARD * median_nn * internal_edges
        - 0.65 * median_nn * same_inside
    )


def grow_compact_blob(seed, size, available, adj, dist, slots, target_point, median_nn):
    """
    Grows a connected, compact cluster.

    The key difference from the older function is that frontier slots with more
    contacts to the current cluster are preferred. That prevents the allocator
    from making a long line when a blob-shaped expansion is available.
    """
    if size <= 0 or seed is None:
        return []

    available = set(available)

    if seed not in available:
        return []

    cluster = [seed]
    cluster_set = {seed}

    while len(cluster) < size:
        frontier = set()

        for c in cluster:
            for nb in adj.get(c, set()):
                if nb in available and nb not in cluster_set:
                    frontier.add(nb)

        if not frontier:
            break

        def candidate_cost(i):
            proposed = cluster + [i]

            pts = np.array([slots[j].center for j in proposed], dtype=np.float32)
            centroid = np.median(pts, axis=0)

            target_distance = float(np.linalg.norm(centroid - target_point))
            radius = float(np.mean(np.linalg.norm(pts - centroid, axis=1)))

            if len(proposed) > 1:
                sub = dist[np.ix_(proposed, proposed)]
                diameter = float(np.max(sub))
            else:
                diameter = 0.0

            contacts = sum(1 for nb in adj.get(i, set()) if nb in cluster_set)

            # Negative contact term is deliberate: a candidate that touches
            # several existing cluster slots makes a 2D patch, not a line.
            return (
                0.30 * target_distance
                + 0.30 * radius
                + 0.20 * diameter
                + 0.20 * float(dist[seed, i])
                - COMPACT_CONTACT_REWARD * median_nn * contacts
            )

        best = min(frontier, key=candidate_cost)
        cluster.append(best)
        cluster_set.add(best)

    return cluster


def choose_compact_cluster(
    slots,
    size,
    candidate_area,
    current_label_indices,
    target_point,
    adj,
    dist,
    median_nn,
):
    """
    Chooses the best exact-size connected cluster among several seed trials.
    """
    if size <= 0:
        return []

    candidate_area = set(candidate_area)

    if not candidate_area:
        return []

    seeds = choose_seed_candidates(
        slots=slots,
        available=candidate_area,
        current_label_indices=current_label_indices,
        target_point=target_point,
        adj=adj,
        dist=dist,
        median_nn=median_nn,
    )

    best_cluster = []
    best_key = None
    same_set = set(current_label_indices)

    for seed in seeds:
        cluster = grow_compact_blob(
            seed=seed,
            size=min(size, len(candidate_area)),
            available=candidate_area,
            adj=adj,
            dist=dist,
            slots=slots,
            target_point=target_point,
            median_nn=median_nn,
        )

        compactness = compactness_score(
            slots=slots,
            cluster=cluster,
            adj=adj,
            dist=dist,
            target_point=target_point,
            current_label_indices=current_label_indices,
            median_nn=median_nn,
        )
        same_inside = sum(i in same_set for i in cluster)
        key = (
            len(cluster) != size,
            -same_inside,
            compactness,
            tuple(sorted(cluster)),
        )

        if best_key is None or key < best_key:
            best_key = key
            best_cluster = cluster

    return best_cluster


def cluster_job_sort_key(job, allocation_order):
    common = (-job["size"],)

    if allocation_order == "top_left":
        position = (job["target_point"][1], job["target_point"][0])
    elif allocation_order == "top_right":
        position = (job["target_point"][1], -job["target_point"][0])
    elif allocation_order == "bottom_left":
        position = (-job["target_point"][1], job["target_point"][0])
    elif allocation_order == "bottom_right":
        position = (-job["target_point"][1], -job["target_point"][0])
    elif allocation_order == "label_forward":
        position = (job["label"], job["part"])
    elif allocation_order == "label_reverse":
        position = tuple(-ord(char) for char in job["label"]) + (job["part"],)
    else:
        raise ValueError(f"unknown cluster allocation order: {allocation_order}")

    return common + position + (job["label"], job["part"])


def make_label_clustered_target_labels(
    slots,
    dist,
    median_nn,
    allocation_order="top_left",
):
    """
    Produces target labels aligned with slot indices.

    Each cluster is exactly one label: same item and same level. If a label has
    more than MAX_CLUSTER_SIZE objects, it is split into multiple compact
    clusters, each with at most MAX_CLUSTER_SIZE slots.

    Example:
        huongduong_1 count = 12

    becomes:
        huongduong_1#1 -> 5 slots
        huongduong_1#2 -> 5 slots
        huongduong_1#3 -> 2 slots

    All three clusters still receive the target label "huongduong_1".
    """
    n = len(slots)
    current_labels = [d.label for d in slots]
    counts = Counter(current_labels)

    target_labels = [None] * n
    available = set(range(n))

    label_indices = {
        label: [i for i, d in enumerate(slots) if d.label == label]
        for label in sorted(counts.keys())
    }

    cluster_jobs = []

    for label in sorted(label_indices.keys()):
        total = counts[label]
        current_idxs = label_indices[label]
        target_point = centroid_of_indices(slots, current_idxs)

        remaining = total
        part = 1

        while remaining > 0:
            size = min(MAX_CLUSTER_SIZE, remaining)
            cluster_name = label if total <= MAX_CLUSTER_SIZE else f"{label}#{part}"

            cluster_jobs.append(
                {
                    "name": cluster_name,
                    "label": label,
                    "size": size,
                    "total": total,
                    "part": part,
                    "current_indices": current_idxs,
                    "target_point": target_point,
                }
            )

            remaining -= size
            part += 1

    # Larger cluster chunks are allocated first because they are harder to fit
    # after smaller chunks fragment the board.
    cluster_jobs.sort(
        key=lambda job: cluster_job_sort_key(job, allocation_order),
    )

    clusters = {}

    for job in cluster_jobs:
        cluster_name = job["name"]
        label = job["label"]
        amount = job["size"]
        current_idxs = job["current_indices"]
        target_point = job["target_point"]

        if not available:
            break

        if amount >= len(available):
            cluster = list(available)
            clusters[cluster_name] = cluster

            for slot_idx in cluster:
                target_labels[slot_idx] = label

            available.clear()
            continue

        chosen_adj = None
        components = []
        enough_components = []
        used_relaxation = ORTHOGONAL_RELAXATION_STEPS[-1]

        for relaxation in ORTHOGONAL_RELAXATION_STEPS:
            chosen_adj = build_cluster_adjacency(
                slots=slots,
                indices=available,
                dist=dist,
                median_nn=median_nn,
                relaxation=relaxation,
            )

            components = connected_components(available, chosen_adj)
            enough_components = [comp for comp in components if len(comp) >= amount]
            used_relaxation = relaxation

            if enough_components:
                break

        if enough_components:
            candidate_area = choose_best_component(
                slots=slots,
                components=enough_components,
                current_label_indices=current_idxs,
                target_point=target_point,
                median_nn=median_nn,
            )
        else:
            candidate_area = (
                set(max(components, key=len)) if components else set(available)
            )

            print(
                f"Warning: {cluster_name} needs {amount} slots, but the largest "
                f"{CLUSTER_CONNECTIVITY} component has {len(candidate_area)} slots. "
                f"Relaxation={used_relaxation}."
            )

        cluster = choose_compact_cluster(
            slots=slots,
            size=min(amount, len(candidate_area)),
            candidate_area=candidate_area,
            current_label_indices=current_idxs,
            target_point=target_point,
            adj=chosen_adj,
            dist=dist,
            median_nn=median_nn,
        )

        # Last-resort defensive fill. If this happens, the available geometry
        # could not produce an exact connected compact cluster for this label.
        while len(cluster) < amount:
            remaining = [i for i in available if i not in set(cluster)]

            if not remaining:
                break

            if cluster:
                extra = min(
                    remaining,
                    key=lambda r: min(float(dist[r, c]) for c in cluster),
                )
            else:
                extra = min(
                    remaining,
                    key=lambda r: float(
                        np.linalg.norm(
                            np.array(slots[r].center, dtype=np.float32) - target_point
                        )
                    ),
                )

            cluster.append(extra)

        clusters[cluster_name] = cluster

        for slot_idx in cluster:
            target_labels[slot_idx] = label

        available -= set(cluster)

    for i in range(n):
        if target_labels[i] is None:
            target_labels[i] = current_labels[i]

    return target_labels, clusters


def cluster_is_connected(slots, indices, dist, median_nn):
    if len(indices) <= 1:
        return True

    adj = build_cluster_adjacency(
        slots=slots,
        indices=indices,
        dist=dist,
        median_nn=median_nn,
        relaxation=1.0,
    )

    return len(connected_components(indices, adj)) <= 1


# ---------------- swap planning ----------------


def _shortest_label_cycles(edge_slots):
    """Returns candidate shortest cycles in the current mismatch graph."""
    adjacency = {}

    for source_label, target_label in edge_slots:
        adjacency.setdefault(source_label, set()).add(target_label)

    cycles = set()
    shortest_length = None

    for first_edge in sorted(edge_slots):
        start_label, next_label = first_edge
        queue = [(next_label, (next_label,))]
        head = 0

        while head < len(queue):
            label, path = queue[head]
            head += 1

            if shortest_length is not None and len(path) >= shortest_length:
                continue

            for following_label in sorted(adjacency.get(label, ())):
                if following_label == start_label:
                    cycle_labels = (start_label,) + path
                    cycle_edges = tuple(
                        (cycle_labels[i], cycle_labels[(i + 1) % len(cycle_labels)])
                        for i in range(len(cycle_labels))
                    )
                    rotations = [
                        cycle_edges[i:] + cycle_edges[:i]
                        for i in range(len(cycle_edges))
                    ]
                    canonical = min(rotations)
                    length = len(canonical)

                    if shortest_length is None or length < shortest_length:
                        shortest_length = length
                        cycles.clear()

                    if length == shortest_length:
                        cycles.add(canonical)

                    continue

                if following_label not in path and following_label != start_label:
                    queue.append((following_label, path + (following_label,)))

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


def optimize_clustered_plan(slots, dist, median_nn):
    """Chooses a connected layout with minimal swaps, then drag distance."""
    current_labels = [slot.label for slot in slots]
    best = None

    for allocation_order in CLUSTER_ALLOCATION_ORDERS:
        target_labels, clusters = make_label_clustered_target_labels(
            slots=slots,
            dist=dist,
            median_nn=median_nn,
            allocation_order=allocation_order,
        )
        swaps = plan_swaps(
            slots=slots,
            current_labels=current_labels,
            target_labels=target_labels,
            dist=dist,
        )
        disconnected_count = sum(
            not cluster_is_connected(slots, indices, dist, median_nn)
            for indices in clusters.values()
        )
        drag_distance = sum(
            float(dist[swap["from_slot"], swap["to_slot"]]) for swap in swaps
        )
        score = (
            disconnected_count,
            len(swaps),
            drag_distance,
            allocation_order,
        )

        if best is None or score < best[0]:
            best = (
                score,
                target_labels,
                clusters,
                swaps,
                allocation_order,
            )

    _, target_labels, clusters, swaps, allocation_order = best
    return target_labels, clusters, swaps, allocation_order


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

    screenshot = pyautogui.screenshot(region=REGION)
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

    slots = stable_sort_slots(detections)

    dist = pairwise_distance_matrix(slots)
    median_nn = median_nearest_neighbor_distance(dist)

    _, clusters, swaps, allocation_order = optimize_clustered_plan(
        slots=slots,
        dist=dist,
        median_nn=median_nn,
    )
    swap_plan_debug_path = save_swap_plan_debug_image(
        screenshot_img,
        slots,
        swaps,
    )

    print(f"Planned {len(swaps)} swaps.")
    print(f"Cluster allocation order: {allocation_order}")
    print(f"Swap plan image: {swap_plan_debug_path}")

    print("\nLabel clusters:")
    for label, indices in clusters.items():
        connected = cluster_is_connected(
            slots=slots,
            indices=indices,
            dist=dist,
            median_nn=median_nn,
        )
        print(
            f"{label}: {len(indices)} slots -> {indices} | "
            f"{CLUSTER_CONNECTIVITY}_connected={connected}"
        )

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
