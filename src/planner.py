import functools
from collections import Counter
import numpy as np

from src.geometry import (
    estimate_isometric_step,
    pairwise_distance_matrix,
    build_isometric_adjacency,
)


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
    if len(current_labels) != len(target_labels) or dist.shape != (
        len(current_labels),
        len(current_labels),
    ):
        raise ValueError("slot labels and distance matrix must have matching sizes")

    if Counter(current_labels) != Counter(target_labels):
        raise ValueError("current and target labels must contain the same items")

    return _plan_swaps(current_labels, target_labels, dist)





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


def get_snake_key(slots):
    """Returns a key function to snake-sort slots (col u, row v snaked)."""
    step_x, step_y, _ = estimate_isometric_step(slots)
    grid_coords = []
    for slot in slots:
        x, y = slot.grid_anchor
        u = 0.5 * (x / step_x + y / step_y)
        v = 0.5 * (y / step_y - x / step_x)
        grid_coords.append((round(u), round(v)))
    return lambda idx: (grid_coords[idx][0], grid_coords[idx][1] if grid_coords[idx][0] % 2 == 0 else -grid_coords[idx][1])


def split_oversized_labels_along_snake(slots, config):
    snake_key = get_snake_key(slots)
    sorted_indices = sorted(range(len(slots)), key=snake_key)
    sorted_labels = [slots[idx].label for idx in sorted_indices]
    
    split_sorted_labels, sub_label_map = split_oversized_labels(sorted_labels, config.max_group_size)
    
    split_labels = [None] * len(slots)
    for sorted_idx, idx in enumerate(sorted_indices):
        split_labels[idx] = split_sorted_labels[sorted_idx]
        
    return split_labels, sub_label_map


def unsplit_labels(labels, sub_label_map):
    """Maps sub-labels back to their original labels."""
    return [sub_label_map.get(label, label) for label in labels]







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



def optimize_isometric_plan(slots, config, strict_sort=False):
    """Plans phase 1 alignment swaps.

    Oversized labels are temporarily split into max_group_size chunks so the
    alignment target keeps mergeable groups separate.

    Returns (target_labels, phase1_swaps, adjacency).
    """
    if strict_sort:
        target_labels, phase1_swaps, adjacency = _optimize_isometric_plan_inner(
            slots, config, strict_sort=True
        )
        return target_labels, phase1_swaps, adjacency

    original_labels = [slot.label for slot in slots]
    max_size = getattr(config, "max_group_size", 5)
    needs_split = max_size > 0 and any(
        count > max_size for count in Counter(original_labels).values()
    )

    if not needs_split:
        target_labels, phase1_swaps, adjacency = _optimize_isometric_plan_inner(
            slots, config, strict_sort=strict_sort
        )
        return target_labels, phase1_swaps, adjacency

    # ── Phase 1: align sub-groups of at most max_size ────────────────────────
    split_labels, sub_label_map = split_oversized_labels_along_snake(slots, config)
    for slot, new_label in zip(slots, split_labels):
        slot.label = new_label

    try:
        phase1_target_split, phase1_swaps, adjacency = _optimize_isometric_plan_inner(
            slots, config, strict_sort=strict_sort
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

def custom_item_sort_key(label):
    base_with_level = label.partition("§")[0]
    
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
        
    PRIORITIZED = {"go": -3, "da": -2, "congcu": -1}
    ANIMALS = {"bo", "ga", "heo", "de", "cuu"}
    if name_part in PRIORITIZED:
        type_flag = PRIORITIZED[name_part]
    elif name_part.startswith("xu"):
        type_flag = 2  # coin at bottom
    elif name_part in ANIMALS:
        type_flag = 1  # animal in middle
    else:
        type_flag = 0  # plant/materials at top
        
    suffix = label.partition("§")[2]
    return (type_flag, name_part, level, suffix)


def partition_into_connected_blocks(comp_slots, adjacency, slots, block_size=5):
    remaining = set(comp_slots)
    blocks = []
    
    while len(remaining) >= block_size:
        start = min(remaining, key=lambda idx: (slots[idx].grid_anchor[1], slots[idx].grid_anchor[0]))
        block = {start}
        queue = [start]
        head = 0
        while head < len(queue) and len(block) < block_size:
            curr = queue[head]
            head += 1
            neighbors = sorted(adjacency[curr] & remaining, key=lambda n: (slots[n].grid_anchor[1], slots[n].grid_anchor[0]))
            for neighbor in neighbors:
                if neighbor not in block:
                    block.add(neighbor)
                    queue.append(neighbor)
                    if len(block) == block_size:
                        break
                        
        if len(block) == block_size:
            remaining.difference_update(block)
            blocks.append(list(block))
        else:
            remaining.remove(start)
            
    leftovers = list(set(comp_slots) - set().union(*(set(b) for b in blocks)))
    return blocks, leftovers


def extract_groups_and_singles(sorted_labels):
    groups = []
    singles = []
    i = 0
    while i < len(sorted_labels):
        if i + 5 <= len(sorted_labels) and len(set(sorted_labels[i:i+5])) == 1:
            groups.append(sorted_labels[i:i+5])
            i += 5
        else:
            singles.append(sorted_labels[i])
            i += 1
    return groups, singles


def optimize_groups_assignment(blocks, group_labels, current_labels, slots):
    """Assigns group_labels to a subset of blocks to minimize mismatches (and break ties by centroid distance)."""
    M = len(blocks)
    G = len(group_labels)
    if M == 0 or G == 0:
        return [None] * M

    # Precompute centroids of slots currently holding each label
    from collections import defaultdict
    label_slots = defaultdict(list)
    for idx, label in enumerate(current_labels):
        label_slots[label].append(idx)
        
    centroids = {}
    for label, indices in label_slots.items():
        if indices:
            xs = [slots[idx].grid_anchor[0] for idx in indices]
            ys = [slots[idx].grid_anchor[1] for idx in indices]
            centroids[label] = (sum(xs) / len(xs), sum(ys) / len(ys))

    # cost_matrix[block_idx][group_idx]
    cost_matrix = []
    for block in blocks:
        # Centroid of this block
        b_xs = [slots[idx].grid_anchor[0] for idx in block]
        b_ys = [slots[idx].grid_anchor[1] for idx in block]
        b_centroid = (sum(b_xs) / len(b_xs), sum(b_ys) / len(b_ys))
        
        row = []
        for label in group_labels:
            mismatches = sum(1 for slot_idx in block if current_labels[slot_idx] != label)
            
            # Distance penalty to break ties
            l_centroid = centroids.get(label, b_centroid)
            dx = b_centroid[0] - l_centroid[0]
            dy = b_centroid[1] - l_centroid[1]
            dist = (dx*dx + dy*dy) ** 0.5
            
            # Combine mismatch with a tiny distance penalty (1e-5)
            cost = mismatches + dist * 1e-5
            row.append(cost)
        cost_matrix.append(row)

    if M <= 10:
        # DFS search to assign G groups to unique blocks out of M blocks
        best_assignment = [None] * M
        best_cost = float("inf")

        assigned_blocks = [False] * M
        current_assignment = [None] * M

        def search(group_idx, current_cost):
            nonlocal best_cost, best_assignment
            if current_cost >= best_cost:
                return

            if group_idx == G:
                best_cost = current_cost
                best_assignment = list(current_assignment)
                return

            for block_idx in range(M):
                if not assigned_blocks[block_idx]:
                    cost = cost_matrix[block_idx][group_idx]
                    assigned_blocks[block_idx] = True
                    current_assignment[block_idx] = group_idx
                    search(group_idx + 1, current_cost + cost)
                    current_assignment[block_idx] = None
                    assigned_blocks[block_idx] = False

        search(0, 0)
        return best_assignment
    else:
        # Greedy fallback if M > 10
        assigned_blocks = [False] * M
        best_assignment = [None] * M
        for group_idx in range(G):
            best_block_idx = -1
            min_cost = float("inf")
            for block_idx in range(M):
                if not assigned_blocks[block_idx]:
                    cost = cost_matrix[block_idx][group_idx]
                    if cost < min_cost:
                        min_cost = cost
                        best_block_idx = block_idx
            if best_block_idx != -1:
                assigned_blocks[best_block_idx] = True
                best_assignment[best_block_idx] = group_idx
        return best_assignment


def connected_components_subset(nodes, adjacency):
    remaining = set(nodes)
    components = []
    while remaining:
        start = next(iter(remaining))
        comp = set()
        queue = [start]
        visited = {start}
        head = 0
        while head < len(queue):
            curr = queue[head]
            head += 1
            comp.add(curr)
            for neighbor in adjacency[curr]:
                if neighbor in remaining and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        remaining.difference_update(comp)
        components.append(list(comp))
    return components


def extract_pre_grouped_blocks(slots, current_labels, adjacency, block_size):
    from collections import defaultdict
    label_slots = defaultdict(list)
    for idx, label in enumerate(current_labels):
        label_slots[label].append(idx)
        
    pre_grouped_blocks = []
    
    for label, indices in label_slots.items():
        if len(indices) < block_size:
            continue
            
        remaining_indices = set(indices)
        while len(remaining_indices) >= block_size:
            # Find connected components of remaining_indices restricted to this label
            visited = set()
            sub_components = []
            for node in remaining_indices:
                if node not in visited:
                    comp = set()
                    queue = [node]
                    visited.add(node)
                    head = 0
                    while head < len(queue):
                        curr = queue[head]
                        head += 1
                        comp.add(curr)
                        for neighbor in adjacency[curr]:
                            if neighbor in remaining_indices and neighbor not in visited:
                                visited.add(neighbor)
                                queue.append(neighbor)
                    sub_components.append(comp)
            
            found_any = False
            for comp in sub_components:
                if len(comp) >= block_size:
                    # Extract a connected block of block_size from this component
                    # Start BFS from the node in comp with minimum coordinates
                    start = min(comp, key=lambda idx: (slots[idx].grid_anchor[1], slots[idx].grid_anchor[0]))
                    block = {start}
                    queue = [start]
                    head = 0
                    while head < len(queue) and len(block) < block_size:
                        curr = queue[head]
                        head += 1
                        neighbors = sorted(adjacency[curr] & comp, key=lambda n: (slots[n].grid_anchor[1], slots[n].grid_anchor[0]))
                        for neighbor in neighbors:
                            if neighbor not in block:
                                block.add(neighbor)
                                queue.append(neighbor)
                                if len(block) == block_size:
                                    break
                                    
                    if len(block) == block_size:
                        pre_grouped_blocks.append((list(block), label))
                        remaining_indices.difference_update(block)
                        found_any = True
                        break # break sub_components to re-find components of remaining_indices
            
            if not found_any:
                break
                
    return pre_grouped_blocks


def get_assignment_cost(S, L, target_labels, current_labels, adjacency):
    B = L.partition("§")[0]
    connected = False
    for nb in adjacency[S]:
        val = target_labels[nb]
        if val is not None and val.partition("§")[0] == B:
            connected = True
            break
    
    cost = 0
    if current_labels[S].partition("§")[0] != B:
        cost += 1
    if connected:
        cost += 1000
    return cost


def assign_singles_optimal(leftover_slots, singles_labels, current_labels, target_labels, adjacency):
    assigned_slots = set()
    for L in singles_labels:
        best_slot = None
        min_cost = float("inf")
        for S in leftover_slots:
            if S not in assigned_slots:
                target_labels[S] = L
                cost = get_assignment_cost(S, L, target_labels, current_labels, adjacency)
                target_labels[S] = None
                if cost < min_cost:
                    min_cost = cost
                    best_slot = S
        if best_slot is not None:
            target_labels[best_slot] = L
            assigned_slots.add(best_slot)


def _optimize_isometric_plan_inner(slots, config, strict_sort=False):
    """Core planner logic, operates on whatever labels slots currently have.
    
    It partitions slots of each connected component into connected blocks of size 5,
    maps sorted 5-groups to these blocks, and maps singles to the leftovers.
    """
    current_labels = [slot.label for slot in slots]
    dist = pairwise_distance_matrix(slots)
    adjacency = build_isometric_adjacency(slots, config)
    
    if strict_sort:
        snake_key = get_snake_key(slots)
        sorted_slots_indices = sorted(range(len(slots)), key=snake_key)
        sorted_labels = sorted(current_labels, key=custom_item_sort_key)
        
        target_labels = [None] * len(slots)
        for slot_idx, label in zip(sorted_slots_indices, sorted_labels):
            target_labels[slot_idx] = label
            
        if target_labels == current_labels:
            return target_labels, [], adjacency
            
        unsplit_current = [label.partition("§")[0] for label in current_labels]
        swaps = plan_swaps(unsplit_current, [label.partition("§")[0] for label in target_labels], dist)
        return target_labels, swaps, adjacency

    # ── Pre-grouped block extraction to preserve already-compact groups ──────
    max_size = getattr(config, "max_group_size", 5)
    
    pre_grouped_blocks = []
    pre_grouped_slots = set()
    pre_grouped_labels_count = Counter()
    
    if max_size > 0:
        pre_grouped = extract_pre_grouped_blocks(slots, current_labels, adjacency, max_size)
        for block, label in pre_grouped:
            pre_grouped_blocks.append((block, label))
            pre_grouped_slots.update(block)
            pre_grouped_labels_count[label] += 1

    # Find remaining slots and their connected components
    remaining_slots_indices = [i for i in range(len(slots)) if i not in pre_grouped_slots]
    components = connected_components_subset(remaining_slots_indices, adjacency)
    # Sort components top-to-bottom by average Y-coordinate
    components.sort(key=lambda comp: sum(slots[i].grid_anchor[1] for i in comp) / len(comp))
    
    # Partition remaining slots of each component into blocks of size max_size and leftovers
    all_blocks = []
    all_leftovers = []
    for comp in components:
        comp_blocks, comp_leftovers = partition_into_connected_blocks(comp, adjacency, slots, block_size=max_size)
        all_blocks.extend(comp_blocks)
        all_leftovers.extend(comp_leftovers)
        
    sorted_labels = sorted(current_labels, key=custom_item_sort_key)
    unsplit_current = [label.partition("§")[0] for label in current_labels]
    
    groups, singles = extract_groups_and_singles(sorted_labels)
    
    # Exclude pre-grouped groups from remaining groups to be assigned
    remaining_groups = []
    temp_pre_grouped_counts = Counter(pre_grouped_labels_count)
    for g in groups:
        label = g[0]
        if temp_pre_grouped_counts[label] > 0:
            temp_pre_grouped_counts[label] -= 1
        else:
            remaining_groups.append(g)
            
    # Handle size mismatches: if we have more groups than blocks, some groups must be split into singles
    if len(all_blocks) < len(remaining_groups):
        extra_groups = remaining_groups[len(all_blocks):]
        remaining_groups = remaining_groups[:len(all_blocks)]
        for group in extra_groups:
            singles.extend(group)
            
    # Map groups to blocks and singles to leftovers
    target_labels = [None] * len(slots)
    
    # Assign pre-grouped blocks
    for block, label in pre_grouped_blocks:
        for slot_idx in block:
            target_labels[slot_idx] = label
            
    # Optimize groups assignment to a subset of blocks
    group_labels = [g[0] for g in remaining_groups]
    best_group_assignments = optimize_groups_assignment(all_blocks, group_labels, current_labels, slots)
    for b_idx, block in enumerate(all_blocks):
        group_idx = best_group_assignments[b_idx]
        if group_idx is not None:
            group = remaining_groups[group_idx]
            for slot_idx, label in zip(block, group):
                target_labels[slot_idx] = label
        else:
            all_leftovers.extend(block)
            
    # Assign singles to leftovers in a way that prevents placing them adjacent to blocks of the same type.
    # Any leftovers not assigned via direct preservation are assigned optimally.
    remaining_singles = Counter(singles)
    
    # First: preserve existing items if they are safe (i.e. not adjacent to blocks of same type)
    unassigned_leftovers = []
    for slot_idx in all_leftovers:
        curr_label = current_labels[slot_idx]
        if remaining_singles[curr_label] > 0:
            # Check if this placement would create a connection
            base = curr_label.partition("§")[0]
            has_connection = False
            for nb in adjacency[slot_idx]:
                val = target_labels[nb]
                if val is not None and val.partition("§")[0] == base:
                    has_connection = True
                    break
            
            if not has_connection:
                target_labels[slot_idx] = curr_label
                remaining_singles[curr_label] -= 1
            else:
                unassigned_leftovers.append(slot_idx)
        else:
            unassigned_leftovers.append(slot_idx)
            
    # Second: assign remaining singles optimally to the remaining unassigned leftovers
    flat_remaining = []
    for label, count in remaining_singles.items():
        flat_remaining.extend([label] * count)
        
    if flat_remaining:
        assign_singles_optimal(unassigned_leftovers, flat_remaining, current_labels, target_labels, adjacency)
        
    if target_labels == current_labels:
        return target_labels, [], adjacency
        
    swaps = plan_swaps(unsplit_current, [label.partition("§")[0] for label in target_labels], dist)
    return target_labels, swaps, adjacency
