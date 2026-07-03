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
    # Generates a 2D isometric grid layout
    points = []
    for row in range(height):
        for col in range(width):
            x = (col - row) * 40
            y = (col + row) * 20
            points.append((x, y, col, row))
    # Sort top-to-bottom, left-to-right
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
    
    # We will test:
    # 1. Original C action + Original X action (but now the production code has the new planner, so we just run new strict sort and new regular plan)
    # Let's verify that running strict_sort=True followed by strict_sort=False results in very few swaps!
    total_swaps = 0
    cases_with_swaps = 0
    
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
        
        if swaps_x:
            cases_with_swaps += 1
            print(f"Case {case}: planned {len(swaps_x)} swaps on sorted board.")
            
    print(f"\nFinal Verification Results over 100 cases:")
    print(f"  Total X-action swaps on strictly sorted boards: {total_swaps}")
    print(f"  Cases with non-zero swaps:                      {cases_with_swaps}/100")

if __name__ == "__main__":
    run_simulation()
