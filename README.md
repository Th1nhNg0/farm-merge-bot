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
level is placed in one connected group. Candidate layouts favor compact groups,
then minimize swaps and drag distance. Any isolated matches outside the main
item grid are excluded as a final safeguard.

Name template variants `<item><level>_<variant>.png`, such as `bo1_1.png` and
`bo1_2.png`. The unsuffixed `<item><level>.png` form also works. Templates should
be tightly cropped around one complete item at the same UI zoom as the board.
