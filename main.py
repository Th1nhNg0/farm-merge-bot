import time
import cv2
import numpy as np
import pyautogui

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
    "go",
    "da",
]

levels = [1, 2, 3]

# Screenshot region: left, top, width, height
REGION = (510, 200, 850, 600)

SCREEN_X0 = REGION[0]
SCREEN_Y0 = REGION[1]

# Detection settings
THRESHOLD = 0.70
NMS_IOU = 0.25

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
MAX_CLUSTER_SIZE = 5

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
MAX_SWAPS = 80
DRAG_DURATION = 0.01
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
                screenshot_hsv,
                template_hsv,
                cv2.TM_CCOEFF_NORMED,
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
    """Removes duplicate overlapping detections globally."""
    if not detections:
        return []

    boxes = [[d.x, d.y, d.w, d.h] for d in detections]
    scores = [d.score for d in detections]

    indices = cv2.dnn.NMSBoxes(
        boxes,
        scores,
        score_threshold=0.0,
        nms_threshold=NMS_IOU,
    )

    if len(indices) == 0:
        return []

    indices = np.array(indices).flatten()
    return [detections[i] for i in indices]


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
    best_score = float("inf")

    for component in components:
        component = set(component)
        component_center = centroid_of_indices(slots, list(component))
        d_to_target = float(np.linalg.norm(component_center - target_point))
        same_inside = sum(1 for i in component if i in same_set)

        score = d_to_target - median_nn * 0.80 * same_inside

        if score < best_score:
            best_score = score
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
            proposed_set = set(proposed)

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
    best_score = float("inf")

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

        score = compactness_score(
            slots=slots,
            cluster=cluster,
            adj=adj,
            dist=dist,
            target_point=target_point,
            current_label_indices=current_label_indices,
            median_nn=median_nn,
        )

        if len(cluster) == size and score < best_score:
            best_score = score
            best_cluster = cluster

        # Keep the best partial cluster as a fallback.
        if not best_cluster and score < best_score:
            best_score = score
            best_cluster = cluster

    return best_cluster


def make_label_clustered_target_labels(slots, dist, median_nn):
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
        key=lambda job: (
            -job["size"],
            job["target_point"][1],
            job["target_point"][0],
            job["label"],
            job["part"],
        )
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


def plan_swaps_nearest(slots, current_labels, target_labels, dist):
    """
    Plans swaps by filling target slots from nearest matching later slots.
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

            if current[j] == target_labels[j]:
                cost += 10000.0

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
    for k, swap in enumerate(swaps[:MAX_SWAPS], start=1):
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


def main():
    time.sleep(1)

    screenshot = pyautogui.screenshot(region=REGION)
    screenshot_img = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

    detections = detect_all_items(screenshot_img)

    print(f"Detected {len(detections)} items.")

    if not detections:
        print("No items detected. Try lowering THRESHOLD.")
        return

    slots = stable_sort_slots(detections)
    current_labels = [d.label for d in slots]

    dist = pairwise_distance_matrix(slots)
    median_nn = median_nearest_neighbor_distance(dist)

    target_labels, clusters = make_label_clustered_target_labels(
        slots=slots,
        dist=dist,
        median_nn=median_nn,
    )

    swaps = plan_swaps_nearest(
        slots=slots,
        current_labels=current_labels,
        target_labels=target_labels,
        dist=dist,
    )

    print(f"Planned {len(swaps)} swaps.")

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
    for i, swap in enumerate(swaps[:MAX_SWAPS], start=1):
        print(
            f"{i}. slot {swap['from_slot']} -> slot {swap['to_slot']} | "
            f"{swap['moving_label']} swaps with {swap['replaced_label']}"
        )

    if DRY_RUN:
        print("\nDRY_RUN is True. No mouse actions were executed.")
        return

    execute_swaps(slots, swaps)


if __name__ == "__main__":
    main()
