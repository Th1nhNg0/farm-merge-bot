import functools
import itertools
import random
from collections import Counter
import numpy as np

from src.geometry import (
    estimate_isometric_step,
    layout_points,
    pairwise_distance_matrix,
    build_isometric_adjacency,
)


@functools.lru_cache(maxsize=256)
def _label_counts(labels):
    return Counter(labels)


def _shortest_label_cycles(edge_slots):
    return _shortest_label_cycles_for_edges(tuple(sorted(edge_slots)))


@functools.lru_cache(maxsize=4096)
def _shortest_label_cycles_for_edges(edges):
    """Returns shortest mismatch cycles using polynomial breadth-first paths."""
    edge_set = set(edges)
    reciprocal_cycles = {
        min(((source, target), (target, source)), ((target, source), (source, target)))
        for source, target in edge_set
        if (target, source) in edge_set
    }

    # A mismatch cycle cannot be shorter than two edges. If reciprocal edges
    # exist, they are therefore the complete set of globally shortest cycles.
    if reciprocal_cycles:
        return sorted(reciprocal_cycles)

    adjacency = {}

    for source_label, target_label in edges:
        adjacency.setdefault(source_label, set()).add(target_label)

    cycles = set()
    shortest_length = None

    for start_label, next_label in edges:
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
                next_states[slot_idx] = min(
                    (
                        cost + float(dist[previous_slot, slot_idx]),
                        path + (slot_idx,),
                    )
                    for previous_slot, (cost, path) in states.items()
                )

            states = next_states

        cost, path = min(states.values())

        if (cost, path) < (best_cost, best_path or ()):
            best_cost = cost
            best_path = path

    return best_cost, best_path


def _plan_swaps(current_labels, target_labels, dist, max_swaps=None):
    target = list(target_labels)
    current = list(current_labels)
    swaps = []

    while current != target:
        edge_slots = {}

        for slot_idx, (current_label, target_label) in enumerate(
            zip(current, target)
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
            if max_swaps is not None and len(swaps) > max_swaps:
                return None

    return swaps


def plan_swaps(current_labels, target_labels, dist):
    """
    Plans all swaps as short mismatch cycles, then minimizes drag distance.

    Correct slots are never disturbed. Short cycles are preferred because each
    independent k-cycle takes only k-1 swaps; reciprocal mismatches therefore
    become one swap instead of being missed by slot-order greedy planning.
    """
    current_key = tuple(current_labels)
    target_key = tuple(target_labels)
    if len(current_key) != len(target_key) or dist.shape != (
        len(current_key),
        len(current_key),
    ):
        raise ValueError("slot labels and distance matrix must have matching sizes")

    if _label_counts(current_key) != _label_counts(target_key):
        raise ValueError("current and target labels must contain the same items")

    return _plan_swaps(current_labels, target_labels, dist)


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


@functools.lru_cache(maxsize=None)
def label_family(label):
    """Returns the item name shared by labels such as bo_1, bo_2, and bo_3."""
    # Strip split-group suffix (e.g. "bo_1§0" -> "bo_1") before extracting family.
    base_label = label.partition("§")[0]
    item, separator, level = base_label.rpartition("_")
    return item if separator and level.isdigit() else base_label


def split_oversized_labels(current_labels, max_group_size):
    """Splits labels into groups of exactly max_group_size.

    Any leftover items (or if total < max_group_size) get unique sub-labels
    so they do not form smaller groups that would auto-merge.
    """
    if max_group_size <= 0:
        return current_labels[:], {label: label for label in set(current_labels)}

    counts = Counter(current_labels)
    seen = {}
    rewritten = []
    sub_label_map = {}
    for label in current_labels:
        idx = seen.get(label, 0)
        seen[label] = idx + 1
        
        complete_groups = counts[label] // max_group_size
        
        if idx < complete_groups * max_group_size:
            sub = f"{label}§{idx // max_group_size}"
        else:
            sub = f"{label}§single{idx}"
            
        rewritten.append(sub)
        sub_label_map[sub] = label
    return rewritten, sub_label_map



def unsplit_labels(labels, sub_label_map):
    """Maps sub-labels back to their original labels."""
    return [sub_label_map.get(label, label) for label in labels]


def _layout_compactness_scorer(slots, adjacency, config):
    """Builds a compactness scorer with board geometry computed once."""
    step_x, step_y, _ = estimate_isometric_step(slots)
    points = layout_points(slots)
    iso_u = 0.5 * ((points[:, 1] / step_y) + (points[:, 0] / step_x))
    iso_v = 0.5 * ((points[:, 1] / step_y) - (points[:, 0] / step_x))
    iso_u_list = iso_u.tolist()
    iso_v_list = iso_v.tolist()

    @functools.lru_cache(maxsize=None)
    def label_score(indices):
        index_set = set(indices)
        u_vals = [iso_u_list[i] for i in indices]
        v_vals = [iso_v_list[i] for i in indices]
        line_group = int(
            len(indices) >= 3
            and (
                (max(u_vals) - min(u_vals)) <= 0.55
                or (max(v_vals) - min(v_vals)) <= 0.55
            )
        )
        contacts = sum(len(adjacency[index] & index_set) for index in indices) // 2
        return line_group, -contacts

    @functools.lru_cache(maxsize=None)
    def family_score(label_groups):
        index_labels = {
            index: group_index
            for group_index, indices in enumerate(label_groups)
            for index in indices
        }
        family_indices = set(index_labels)
        pending_indices = set(family_indices)
        component_count = 0

        while pending_indices:
            component_count += 1
            pending = [next(iter(pending_indices))]

            while pending:
                index = pending.pop()

                if index not in pending_indices:
                    continue

                pending_indices.remove(index)
                pending.extend(adjacency[index] & pending_indices)

        cross_level_contacts = sum(
            1
            for left in family_indices
            for right in adjacency[left]
            if left < right
            and right in family_indices
            and index_labels[left] != index_labels[right]
        )
        return component_count - 1, -cross_level_contacts

    def score(target_labels):
        label_indices = {}

        for index, label in enumerate(target_labels):
            label_indices.setdefault(label, []).append(index)

        line_groups, internal_contacts = tuple(
            sum(values)
            for values in zip(
                *(label_score(tuple(indices)) for indices in label_indices.values())
            )
        )
        family_groups = {}

        for label, indices in label_indices.items():
            family_groups.setdefault(label_family(label), []).append(tuple(indices))

        family_disconnects, cross_level_contacts = tuple(
            sum(values)
            for values in zip(
                *(
                    family_score(tuple(sorted(groups)))
                    for groups in family_groups.values()
                )
            )
        )
        return (
            family_disconnects,
            line_groups,
            cross_level_contacts,
            internal_contacts,
        )

    return score


def layout_compactness_score(slots, target_labels, adjacency, config):
    """Penalizes straight isometric lines, then rewards cardinal contacts."""
    score = _layout_compactness_scorer(slots, adjacency, config)

    # Lower scores are better. Lines are rejected before contact maximization.
    return score(target_labels)


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


def candidate_label_orders(current_labels, config):
    """Yields exhaustive small-board orders and deterministic large-board trials."""
    labels = tuple(sorted(set(current_labels)))

    if len(labels) <= config.exact_label_order_limit:
        return itertools.permutations(labels)

    counts = Counter(current_labels)
    candidates = [
        labels,
        tuple(reversed(labels)),
        tuple(sorted(labels, key=lambda label: (-counts[label], label))),
        tuple(sorted(labels, key=lambda label: (counts[label], label))),
    ]
    rng = random.Random(config.label_order_seed)

    for _ in range(config.label_order_trials):
        shuffled = list(labels)
        rng.shuffle(shuffled)
        candidates.append(tuple(shuffled))

    return iter(dict.fromkeys(candidates))


def target_labels_for_scan(current_labels, scan_order, label_order, counts=None):
    """Places each label in one connected segment of a scan path."""
    counts = counts or Counter(current_labels)
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
        options = sorted(sizes)
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
        ordered_segments = []
        offset = 0

        for size in size_order:
            segment = scan_order[offset : offset + size]
            segments_by_size.setdefault(size, []).append(segment)
            ordered_segments.append((size, segment))
            offset += size

        for trial in range(10):
            target = [None] * len(current_labels)

            if trial in (0, 1, 2):
                available_by_size = {
                    size: sorted(
                        (
                            label
                            for label, count in label_counts.items()
                            if count == size
                        ),
                        reverse=trial == 2,
                    )
                    for size in segments_by_size
                }
                active_family = None

                for size, segment in ordered_segments:
                    available_labels = available_by_size[size]
                    if trial == 0:
                        label = available_labels[0]
                        available_labels.remove(label)

                        for index in segment:
                            target[index] = label

                        continue

                    matching_family = [
                        label
                        for label in available_labels
                        if label_family(label) == active_family
                    ]

                    if matching_family:
                        label = matching_family[0]
                    else:
                        label = available_labels[0]
                        active_family = label_family(label)

                    available_labels.remove(label)

                    for index in segment:
                        target[index] = label

                yield target
                continue

            for size, segments in segments_by_size.items():
                available_labels = sorted(
                    label for label, count in label_counts.items() if count == size
                )

                for segment in segments:
                    label = rng.choice(available_labels)

                    available_labels.remove(label)

                    for index in segment:
                        target[index] = label

            yield target


def grow_connected_target(current_labels, adjacency, rng):
    """Grows one cardinally connected region per label from current-label seeds."""
    counts = Counter(current_labels)
    label_order = sorted(counts)
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


def plan_merge_triggers(target_labels, adjacency, max_group_size):
    """Plans drags from one item into a connected group of max_group_size - 1.

    Returns a list of {from_slot, to_slot, label} dicts.
    """
    label_slots = {}
    for slot_idx, label in enumerate(target_labels):
        label_slots.setdefault(label, []).append(slot_idx)

    triggers = []

    for label, slot_indices in label_slots.items():
        # Find connected components of this label
        components = []
        unvisited = set(slot_indices)
        while unvisited:
            start = next(iter(unvisited))
            component = []
            pending = [start]
            unvisited.remove(start)
            while pending:
                curr = pending.pop()
                component.append(curr)
                for neighbor in adjacency[curr]:
                    if neighbor in unvisited:
                        unvisited.remove(neighbor)
                        pending.append(neighbor)
            components.append(component)

        target_group_size = max_group_size - 1

        for component_slots in components:
            if len(component_slots) < max_group_size:
                continue

            found = False
            component_set = set(component_slots)

            for from_slot in sorted(component_slots):
                remaining = component_set - {from_slot}
                groups = []

                while remaining:
                    group = set()
                    pending = [remaining.pop()]

                    while pending:
                        idx = pending.pop()
                        group.add(idx)
                        next_slots = adjacency[idx] & remaining
                        remaining.difference_update(next_slots)
                        pending.extend(next_slots)

                    groups.append(group)

                target_groups = [
                    group for group in groups if len(group) == target_group_size
                ]
                if not target_groups:
                    continue

                to_slot = min(min(target_groups))
                triggers.append(
                    {
                        "from_slot": from_slot,
                        "to_slot": to_slot,
                        "label": label,
                    }
                )
                found = True
                break

            if not found:
                print(
                    f"[merge] WARNING: component of '{label}' (slots {component_slots}) "
                    "has no connected 4-item target group — skipping merge."
                )

    return triggers



def optimize_isometric_plan(slots, config):
    """Plans phase 1 alignment swaps.

    Oversized labels are temporarily split into max_group_size chunks so the
    alignment target keeps mergeable groups separate.

    Returns (target_labels, phase1_swaps, adjacency).
    """
    original_labels = [slot.label for slot in slots]
    max_size = getattr(config, "max_group_size", 5)
    needs_split = max_size > 0 and any(
        count > max_size for count in Counter(original_labels).values()
    )

    if not needs_split:
        target_labels, phase1_swaps, adjacency = _optimize_isometric_plan_inner(
            slots, config
        )
        return target_labels, phase1_swaps, adjacency

    # ── Phase 1: align sub-groups of at most max_size ────────────────────────
    split_labels, sub_label_map = split_oversized_labels(original_labels, max_size)
    for slot, new_label in zip(slots, split_labels):
        slot.label = new_label

    try:
        phase1_target_split, phase1_swaps, adjacency = _optimize_isometric_plan_inner(
            slots, config
        )
    finally:
        for slot, orig in zip(slots, original_labels):
            slot.label = orig

    # Map sub-labels in phase 1 swap descriptions back to original labels.
    phase1_target = unsplit_labels(phase1_target_split, sub_label_map)
    for swap in phase1_swaps:
        swap["moving_label"] = sub_label_map.get(
            swap["moving_label"], swap["moving_label"]
        )
        swap["replaced_label"] = sub_label_map.get(
            swap["replaced_label"], swap["replaced_label"]
        )

    return phase1_target, phase1_swaps, adjacency


def _optimize_isometric_plan_inner(slots, config):
    """Core planner logic, operates on whatever labels slots currently have."""
    current_labels = [slot.label for slot in slots]
    dist = pairwise_distance_matrix(slots)
    adjacency = build_isometric_adjacency(slots, config)
    compactness_score = _layout_compactness_scorer(slots, adjacency, config)
    scan_orders = orthogonal_scan_orders(slots)
    candidates = {}
    rejected_candidates = set()
    label_counts = Counter(current_labels)

    def add_candidate(target_labels):
        target_key = tuple(target_labels)

        if target_key in candidates or target_key in rejected_candidates:
            return
        if not labels_are_cardinally_connected(target_labels, adjacency):
            rejected_candidates.add(target_key)
            return

        cheap_score = (
            compactness_score(target_labels),
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
        for label_order in candidate_label_orders(current_labels, config):
            add_candidate(
                target_labels_for_scan(
                    current_labels,
                    scan_order,
                    label_order,
                    label_counts,
                )
            )

    rng = random.Random(config.label_order_seed)

    for scan_order in scan_orders:
        for target_labels in candidate_targets_for_scan(
            current_labels,
            scan_order,
            adjacency,
            rng,
        ):
            add_candidate(target_labels)

    if not candidates:
        for _ in range(config.connected_region_trials):
            target_labels = grow_connected_target(current_labels, adjacency, rng)

            if target_labels is not None:
                add_candidate(target_labels)

    if not candidates:
        raise RuntimeError(
            "could not allocate connected isometric top/right/bottom/left item regions"
        )

    best_compactness = min(candidate[0][0] for candidate in candidates.values())
    effective_candidates = [
        candidate
        for candidate in candidates.values()
        if candidate[0][0] == best_compactness
    ]
    ordered_candidates = sorted(
        effective_candidates,
        key=lambda candidate: (candidate[0][1], candidate[0][2]),
    )
    _, target_labels = ordered_candidates[0]
    swaps = plan_swaps(current_labels, target_labels, dist)

    return target_labels, swaps, adjacency
