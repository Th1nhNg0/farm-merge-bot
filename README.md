# Auto Farm

## Detection calibration

Capture a board and inspect detections without moving any items:

```bash
uv run python main.py --detect-only
```

The application captures and scans the full screen. Detection coordinates are
used directly as mouse coordinates; no screenshot crop or coordinate offset is
required.

Generate and print the complete adjacency/swap plan without moving any items:

```bash
uv run python main.py --dry-run
```

The command writes:

- `debug/board.png`: the raw configured screenshot region.
- `debug/detections.png`: detected rectangles, labels, and confidence scores.
- `debug/scores.csv`: best score and selected template size for every label,
  including labels that did not pass their threshold.

Detected slots are connected only through their nearest top, right, bottom, and
left neighbors within one inferred cell step; diagonal contact and gaps over a
missing cell do not count. Each identical item and level is placed in one
orthogonally connected group. Candidate layouts avoid straight groups of three
or more items when a thicker connected block is possible, then maximize compact
orthogonal contact, minimize swaps, and minimize drag distance. Label orders are
exhaustive for up to eight distinct labels and use deterministic bounded search
above that limit to avoid factorial runtime. Isolated full-screen matches
outside the main orthogonal item grid are excluded from swap planning.

Name template variants `<item><level>_<variant>.png`, such as `bo1_1.png` and
`bo1_2.png`. The unsuffixed `<item><level>.png` form is also supported for
backward compatibility. All variants for an item and level are matched as the
same logical label.

Templates should be tightly cropped around one complete item at the same UI
zoom as the board. Avoid neighboring item fragments and unnecessary background.
