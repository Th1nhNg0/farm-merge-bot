import functools
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





def label_sort_key(label):
    """Returns a sort key for a sub-label.

    1. Places 5-groups (e.g. 'bo_1§0') before singles (e.g. 'bo_1§single5').
    2. Sorts alphabetically by the base label (e.g. 'bo_1', 'ga_1').
    3. Sorts by the suffix part (e.g. group index or item index) as a tie-breaker.
    """
    base = label.partition("§")[0]
    suffix = label.partition("§")[2]
    is_single = 1 if "§single" in label else 0
    return (is_single, base, suffix)



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

    return list(dict.fromkeys(candidates))




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


def _plan_merge_trigger_for_group(label, group_slots, adjacency, max_group_size):
    target_group_size = max_group_size - 1
    group_set = set(group_slots)

    for from_slot in sorted(group_slots):
        remaining = group_set - {from_slot}
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
        if target_groups:
            return {
                "from_slot": from_slot,
                "to_slot": min(min(target_groups)),
                "label": label,
            }

    return None


def plan_merge_triggers(target_labels, adjacency, max_group_size):
    """Plans drags for connected components whose size is a max_group_size multiple.

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

        for component_slots in components:
            if (
                len(component_slots) < max_group_size
                or len(component_slots) % max_group_size != 0
            ):
                continue

            found = False
            for offset in range(0, len(component_slots), max_group_size):
                group_slots = sorted(component_slots)[offset : offset + max_group_size]
                if len(group_slots) < max_group_size:
                    continue

                trigger = _plan_merge_trigger_for_group(
                    label, group_slots, adjacency, max_group_size
                )
                if trigger is None:
                    continue

                triggers.append(trigger)
                found = True

            if not found:
                print(
                    f"[merge] WARNING: component of '{label}' (slots {component_slots}) "
                    f"has no connected {max_group_size - 1}-item target group — skipping merge."
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


def partition_scan_order(scan_order, label_counts, adjacency):
    """Finds a partition of scan_order into connected segments matching label counts.
    
    Returns a list of sizes, or None.
    """
    remaining_sizes = Counter(label_counts.values())
    failed_states = set()
    result = []

    def search(offset, sizes, current_path):
        state = (offset, tuple(sorted(sizes.items())))
        if state in failed_states:
            return False
        if not sizes:
            result.append(current_path)
            return True

        options = sorted(sizes, reverse=True)

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

            if search(offset + size, next_sizes, current_path + (size,)):
                return True

        failed_states.add(state)
        return False

    if search(0, remaining_sizes, ()):
        return result[0]
    return None


def target_labels_from_size_order(scan_order, size_order, current_labels):
    """Constructs target labels by placing sorted sub-labels in the partitioned scan order."""
    label_counts = Counter(current_labels)
    
    # Group label names by count
    labels_by_size = {}
    for label, count in label_counts.items():
        labels_by_size.setdefault(count, []).append(label)
        
    # Sort them using label_sort_key
    for size in labels_by_size:
        labels_by_size[size].sort(key=label_sort_key)
        
    labels_iter = {size: iter(labels_by_size[size]) for size in labels_by_size}
    
    target_labels = [None] * len(current_labels)
    offset = 0
    for size in size_order:
        label = next(labels_iter[size])
        for slot_idx in scan_order[offset : offset + size]:
            target_labels[slot_idx] = label
        offset += size
        
    return target_labels


def custom_item_sort_key(label):
    base_with_level = label.partition("§")[0]
    is_coin = base_with_level.startswith("xu")
    coin_flag = 1 if is_coin else 0
    
    is_single = 1 if "§single" in label else 0
    
    if "_" in base_with_level:
        name_part, _, level_part = base_with_level.rpartition("_")
        try:
            level = int(level_part)
        except ValueError:
            name_part = base_with_level
            level = 0
    else:
        name_part = base_with_level
        level = 0
        
    suffix = label.partition("§")[2]
    return (coin_flag, is_single, name_part, level, suffix)


def _optimize_isometric_plan_inner(slots, config):
    """Core planner logic, operates on whatever labels slots currently have.
    
    It finds a snake scan order that maps sorted labels to slots such that
    every group is cardinally connected, and coins are positioned at the bottom.
    """
    current_labels = [slot.label for slot in slots]
    dist = pairwise_distance_matrix(slots)
    adjacency = build_isometric_adjacency(slots, config)
    scan_orders = orthogonal_scan_orders(slots)
    
    sorted_labels = sorted(current_labels, key=custom_item_sort_key)
    unsplit_current = [label.partition("§")[0] for label in current_labels]
    
    candidates = []
    
    for scan_order in scan_orders:
        if len(scan_order) != len(slots):
            continue
            
        target_labels = [None] * len(slots)
        for i, slot_idx in enumerate(scan_order):
            target_labels[slot_idx] = sorted_labels[i]
            
        # Verify that all item groups are cardinally connected
        if not labels_are_cardinally_connected(target_labels, adjacency):
            continue
            
        # Short-circuit if already in target layout
        if target_labels == current_labels:
            return target_labels, [], adjacency
            
        unsplit_target = [label.partition("§")[0] for label in target_labels]
        swaps = plan_swaps(unsplit_current, unsplit_target, dist)
        total_dist = sum(dist[swap["from_slot"], swap["to_slot"]] for swap in swaps)
        
        # Calculate coin positioning score (average Y of coins - average Y of non-coins)
        coin_ys = [slots[i].grid_anchor[1] for i, label in enumerate(target_labels) if label.partition("§")[0].startswith("xu")]
        non_coin_ys = [slots[i].grid_anchor[1] for i, label in enumerate(target_labels) if not label.partition("§")[0].startswith("xu")]
        
        if coin_ys and non_coin_ys:
            coin_score = sum(coin_ys) / len(coin_ys) - sum(non_coin_ys) / len(non_coin_ys)
        else:
            coin_score = 0.0
            
        candidates.append((coin_score, len(swaps), total_dist, target_labels, swaps))
        
    if not candidates:
        # Fallback to simple sorted layout if no connected layout can be found
        target_labels = sorted_labels
        unsplit_target = [label.partition("§")[0] for label in target_labels]
        swaps = plan_swaps(unsplit_current, unsplit_target, dist)
        return target_labels, swaps, adjacency
        
    # Choose candidate prioritizing coin_score >= 0, then minimizing swaps, then distance
    best_candidate = min(
        candidates,
        key=lambda c: (
            1 if c[0] < 0 else 0, # coin_score >= 0 is preferred
            c[1],                 # number of swaps
            c[2]                  # total drag distance
        )
    )
    
    _, _, _, target_labels, swaps = best_candidate
    return target_labels, swaps, adjacency
