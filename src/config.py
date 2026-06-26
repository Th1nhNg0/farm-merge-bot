from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    items: list[str] = field(
        default_factory=lambda: [
            "bo",
            "carot",
            "daunanh",
            "ga",
            "heo",
            "huongduong",
            "luami",
            "mia",
            "de",
            "bap",
            "go",
            "da",
            "congcu",
            "cuu",
            "xu",
        ]
    )
    levels: list[int] = field(default_factory=lambda: [1, 2, 3])
    item_levels: dict[str, list[int]] = field(
        default_factory=lambda: {
            "go": [1, 2, 3, 4, 5],
            "da": [1, 2, 3, 4, 5],
            "congcu": [1, 2, 3, 4, 5],
            "xu": [1, 2, 3, 4, 5, 6],
        }
    )
    window_title: str | None = None

    # Detection settings
    threshold: float = 0.70
    template_dir: Path = Path("images")
    template_scales: tuple[float, ...] = (1.00,)
    template_thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "go_1": 0.62,
        }
    )
    grayscale_score_weight: float = 0.70
    edge_score_weight: float = 0.30
    local_max_kernel: int = 5
    duplicate_center_factor: float = 0.35
    min_duplicate_center_distance: float = 6.0
    detection_debug_dir: Path = Path("debug")

    # Viewport detection settings
    game_min_saturation: int = 30
    game_min_brightness: int = 30
    game_min_screen_area: float = 0.10
    game_min_fill_ratio: float = 0.50
    game_crop_padding: int = 2

    # Isometric layout settings
    grid_anchor_y_factor: float = 0.72
    isometric_axis_tolerance: float = 0.65
    isometric_min_step_factor: float = 0.45
    isometric_max_step_factor: float = 1.70
    exact_label_order_limit: int = 6
    label_order_trials: int = 96
    label_order_seed: int = 20260619
    connected_region_trials: int = 8
    max_group_size: int = 5

    # Swap settings
    drag_duration: float = 0.0015
    swap_settle_delay: float = 0.01
    after_swap_delay: float = 0.01
