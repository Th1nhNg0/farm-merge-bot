import math
import numpy as np


def pairwise_distance_matrix(slots):
    centers = np.array([d.center for d in slots], dtype=np.float32)
    diff = centers[:, None, :] - centers[None, :, :]
    return np.linalg.norm(diff, axis=2)


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

    step_distance = float(math.hypot(step_x, step_y))
    return step_x, step_y, max(step_distance, 1.0)


def build_isometric_adjacency(slots, config):
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
            if abs(dx) < step_x * config.isometric_min_step_factor:
                continue
            if abs(dy) < step_y * config.isometric_min_step_factor:
                continue

            norm_dx = abs(dx) / step_x
            norm_dy = abs(dy) / step_y
            normalized_step = max(norm_dx, norm_dy)
            axis_error = abs(norm_dx - norm_dy)

            if normalized_step > config.isometric_max_step_factor:
                continue
            if axis_error > config.isometric_axis_tolerance:
                continue

            distance = float(math.hypot(dx, dy))
            if distance > step_distance * config.isometric_max_step_factor:
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


