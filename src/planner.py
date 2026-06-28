import functools
import random
from collections import Counter
import numpy as np

from src.geometry import (
    estimate_isometric_step,
    layout_points,
    pairwise_distance_matrix,
    build_isometric_adjacency,
    connected_components,
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

def custom_item_sort_key(label):
    base_with_level = label.partition("§")[0]
    is_coin = base_with_level.startswith("xu")
    coin_flag = 1 if is_coin else 0
    
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
    return (coin_flag, name_part, level, suffix)


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


def _optimize_isometric_plan_inner(slots, config):
    """Core planner logic, operates on whatever labels slots currently have.
    
    It partitions slots of each connected component into connected blocks of size 5,
    maps sorted 5-groups to these blocks, and maps singles to the leftovers.
    """
    current_labels = [slot.label for slot in slots]
    dist = pairwise_distance_matrix(slots)
    adjacency = build_isometric_adjacency(slots, config)
    
    # Partition slots into connected components (islands)
    components = [list(c) for c in connected_components(adjacency)]
    # Sort components top-to-bottom by average Y-coordinate
    components.sort(key=lambda comp: sum(slots[i].grid_anchor[1] for i in comp) / len(comp))
    
    # Partition slots of each component into blocks of size 5 and leftovers
    all_blocks = []
    all_leftovers = []
    for comp in components:
        comp_blocks, comp_leftovers = partition_into_connected_blocks(comp, adjacency, slots)
        all_blocks.extend(comp_blocks)
        all_leftovers.extend(comp_leftovers)
        
    sorted_labels = sorted(current_labels, key=custom_item_sort_key)
    unsplit_current = [label.partition("§")[0] for label in current_labels]
    
    groups, singles = extract_groups_and_singles(sorted_labels)
    
    # Handle size mismatches between blocks and groups
    if len(all_blocks) > len(groups):
        extra_blocks = all_blocks[len(groups):]
        all_blocks = all_blocks[:len(groups)]
        for block in extra_blocks:
            all_leftovers.extend(block)
    elif len(all_blocks) < len(groups):
        extra_groups = groups[len(all_blocks):]
        groups = groups[:len(all_blocks)]
        for group in extra_groups:
            singles.extend(group)
            
    # Sort blocks and leftovers top-to-bottom
    all_blocks.sort(key=lambda b: sum(slots[i].grid_anchor[1] for i in b) / len(b))
    all_leftovers.sort(key=lambda idx: (slots[idx].grid_anchor[1], slots[idx].grid_anchor[0]))
    
    # Map groups to blocks and singles to leftovers
    target_labels = [None] * len(slots)
    for b_idx, block in enumerate(all_blocks):
        group = groups[b_idx]
        for slot_idx, label in zip(block, group):
            target_labels[slot_idx] = label
            
    for s_idx, slot_idx in enumerate(all_leftovers):
        target_labels[slot_idx] = singles[s_idx]
        
    if target_labels == current_labels:
        return target_labels, [], adjacency
        
    swaps = plan_swaps(unsplit_current, [label.partition("§")[0] for label in target_labels], dist)
    return target_labels, swaps, adjacency
