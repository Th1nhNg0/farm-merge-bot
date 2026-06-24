# Auto Farm

Open the game, then run:

```bash
uv run python main.py
```

No command-line arguments or run-mode settings are needed. The application
automatically finds and crops the colorful game viewport before template
matching. It retains the crop offset, so mouse actions still use correct
full-screen coordinates. This avoids matching Discord, terminals, and other
windows around the game.

Every run writes the cropped screenshot to `debug/board.png`, its annotated
detections to `debug/detections.png`, and matching details to
`debug/scores.csv` before executing swaps.

## Detection

Detected slots are connected only through their nearest top, right, bottom, and
left neighbors within one inferred isometric cell step. Each identical item and
level is placed in one connected group. Groups belonging to the same item stay
adjacent across levels whenever the board topology allows it. Candidate layouts
preserve the best family grouping and compactness score, then minimize swaps
with a cheap mismatch score. Exact swap planning runs once for the chosen
layout because mouse movement is fast enough to tolerate a few extra swaps.

Name template variants `<item><level>_<variant>.png`, such as `bo1_1.png` and
`bo1_2.png`. The unsuffixed `<item><level>.png` form also works. Templates should
be tightly cropped around one complete item at the same UI zoom as the board.

Repeated runs keep an already compact connected layout unchanged, producing
zero swaps unless the detected board contents change.
