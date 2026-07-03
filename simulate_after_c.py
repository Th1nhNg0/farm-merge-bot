import sys
import types
from collections import Counter
from src.config import Config
import src.geometry as geometry
import src.planner as planner

def make_slots(labels, points):
    return [
        types.SimpleNamespace(
            label=label,
            center=point,
            grid_anchor=point,
            w=80,
            h=40,
        )
        for label, point in zip(labels, points)
    ]

def generate_isometric_grid(width=5, height=5):
    points = []
    for row in range(height):
        for col in range(width):
            x = (col - row) * 40
            y = (col + row) * 20
            points.append((x, y, col, row))
    points.sort(key=lambda p: (p[1], p[0]))
    return [(p[0], p[1]) for p in points]

def run_simulation():
    config = Config()
    points = generate_isometric_grid(6, 6) # 36 slots
    
    item_pool = [
        "go_1", "da_1", "congcu_1", "bo_1", "ga_1", "carot_1", "xu_1", "mia_1"
    ]
    
    import random
    random.seed(42)
    
    total_swaps = 0
    total_merges = 0
    wasted_merges = 0
    
    for case in range(1, 101):
        labels = []
        for item in item_pool:
            count = random.randint(1, 8)
            labels.extend([item] * count)
            
        labels = labels[:len(points)]
        random.shuffle(labels)
        
        # 1. Run strict sort (C action)
        slots_c = make_slots(labels, points[:len(labels)])
        _, swaps_c, _ = planner.optimize_isometric_plan(slots_c, config, strict_sort=True)
        sorted_labels = labels[:]
        for swap in swaps_c:
            f, t = swap["from_slot"], swap["to_slot"]
            sorted_labels[f], sorted_labels[t] = sorted_labels[t], sorted_labels[f]
            
        # 2. Run regular alignment plan (X action) on the sorted board
        slots_x = make_slots(sorted_labels, points[:len(sorted_labels)])
        _, swaps_x, _ = planner.optimize_isometric_plan(slots_x, config, strict_sort=False)
        total_swaps += len(swaps_x)
        
        # 3. Apply swaps to simulate final board layout
        final_labels = sorted_labels[:]
        for swap in swaps_x:
            f, t = swap["from_slot"], swap["to_slot"]
            final_labels[f], final_labels[t] = final_labels[t], final_labels[f]
            
        # 4. Plan merges on the final layout
        slots_final = make_slots(final_labels, points[:len(final_labels)])
        adjacency_final = geometry.build_isometric_adjacency(slots_final, config)
        merge_triggers = planner.plan_merge_triggers(final_labels, adjacency_final, config.max_group_size)
        total_merges += len(merge_triggers)
        
        # Verify that all connected components of the final board are multiples of 5,
        # or if not, they are NOT merged.
        # More specifically, check if any merge trigger belongs to a component of size not divisible by 5
        label_slots = {}
        for idx, label in enumerate(final_labels):
            label_slots.setdefault(label, []).append(idx)
            
        for label, slot_indices in label_slots.items():
            unvisited = set(slot_indices)
            while unvisited:
                start = next(iter(unvisited))
                component = []
                pending = [start]
                unvisited.remove(start)
                while pending:
                    curr = pending.pop()
                    component.append(curr)
                    for neighbor in adjacency_final[curr]:
                        if neighbor in unvisited:
                            unvisited.remove(neighbor)
                            pending.append(neighbor)
                
                # Check if this component has a planned merge trigger
                has_trigger = any(tr["from_slot"] in component for tr in merge_triggers)
                if has_trigger:
                    if len(component) % 5 != 0:
                        wasted_merges += 1
                        print(f"Case {case}: Wasted merge! Component size {len(component)} of '{label}' has a merge trigger!")
                        
    print(f"\nFinal Verification Results over 100 cases:")
    print(f"  Total X-action swaps on strictly sorted boards: {total_swaps}")
    print(f"  Total merge triggers planned:                  {total_merges}")
    print(f"  Total wasted merges (sizes not divisible by 5): {wasted_merges}")

if __name__ == "__main__":
    run_simulation()
