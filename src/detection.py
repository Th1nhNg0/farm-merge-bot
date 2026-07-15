from dataclasses import dataclass
import time
import cv2
import numpy as np
import mss
import pygetwindow as gw


_prepared_templates_cache = {}
_scale_cache = {}


def focus_game_window(config):
    """Finds the configured or Discord window, restores and activates it."""
    title = config.window_title
    if not title:
        titles = [t for t in gw.getAllTitles() if "discord" in t.lower()]
        if titles:
            title = titles[0]
            config.window_title = title
        else:
            print("Warning: Could not auto-detect Discord window.")

    if title:
        windows = gw.getWindowsWithTitle(title)
        if windows:
            win = windows[0]
            if win.width > 0 and win.height > 0:
                if win.isMinimized:
                    win.restore()
                try:
                    win.activate()
                except Exception:
                    pass
                time.sleep(0.5)
                return win
    return None



@dataclass
class Detection:
    label: str
    x: int
    y: int
    w: int
    h: int
    score: float
    grid_anchor_y_factor: float = 0.72

    @property
    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def grid_anchor(self):
        """Approximate tile anchor used only for isometric geometry."""
        return self.x + self.w // 2, self.y + int(round(self.h * self.grid_anchor_y_factor))


@dataclass(frozen=True)
class PreparedTemplate:
    """Template variant cached in all expensive preprocessing forms."""

    label: str
    w: int
    h: int
    features: tuple





def template_paths(item, level, config):
    """Returns every template variant for an item level."""
    base_name = f"{item}{level}"
    return sorted(
        path
        for path in config.template_dir.glob(f"{base_name}*.png")
        if path.stem == base_name or path.stem.startswith(f"{base_name}_")
    )


def matching_features(image):
    """Builds illumination-tolerant intensity and shape features."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 45, 135)
    return gray, edges


def combined_match_score(screenshot_features, template_features, config):
    """Scores a cached template against a screenshot feature pair."""
    template_gray, template_edges = template_features
    screenshot_gray, screenshot_edges = screenshot_features

    gray_score = cv2.matchTemplate(
        screenshot_gray,
        template_gray,
        cv2.TM_CCOEFF_NORMED,
    )

    edge_score = cv2.matchTemplate(
        screenshot_edges,
        template_edges,
        cv2.TM_CCOEFF_NORMED,
    )
    edge_score = np.nan_to_num(edge_score, nan=0.0, posinf=0.0, neginf=0.0)

    return config.grayscale_score_weight * gray_score + config.edge_score_weight * edge_score


def scaled_templates(template, config):
    """Yields unique configured template sizes, including the original size."""
    original_h, original_w = template.shape[:2]
    seen_sizes = set()

    for scale in config.template_scales:
        width = max(3, int(round(original_w * scale)))
        height = max(3, int(round(original_h * scale)))

        if (width, height) in seen_sizes:
            continue

        seen_sizes.add((width, height))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        yield cv2.resize(template, (width, height), interpolation=interpolation)


def deduplicate_detections(detections, config):
    """Keeps the best label near each center without removing nearby slots."""
    kept = []

    for candidate in sorted(detections, key=lambda d: d.score, reverse=True):
        duplicate = False

        for accepted in kept:
            dx = candidate.center[0] - accepted.center[0]
            dy = candidate.center[1] - accepted.center[1]
            center_distance = float(np.hypot(dx, dy))
            reference_size = min(
                candidate.w,
                candidate.h,
                accepted.w,
                accepted.h,
            )
            duplicate_distance = max(
                config.min_duplicate_center_distance,
                reference_size * config.duplicate_center_factor,
            )

            if center_distance <= duplicate_distance:
                duplicate = True
                break

        if not duplicate:
            kept.append(candidate)

    return kept


def configured_labels(config):
    """Yields every configured item-level label in detection order."""
    for item in config.items:
        for level in config.item_levels.get(item, config.levels):
            yield item, level, f"{item}_{level}"


def load_prepared_templates(config):
    """Reads and preprocesses templates."""
    key = (str(config.template_dir), config.template_scales)
    if key in _prepared_templates_cache:
        return _prepared_templates_cache[key]

    labels = tuple(configured_labels(config))
    prepared = []
    missing = []

    for item, level, label in labels:
        paths = template_paths(item, level, config)

        if not paths:
            missing.append((item, level, label))
            continue

        for template_path in paths:
            template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)

            if template is None:
                print(f"Skipping unreadable template: {template_path}")
                continue

            for scaled_template in scaled_templates(template, config):
                th, tw = scaled_template.shape[:2]
                prepared.append(
                    PreparedTemplate(
                        label=label,
                        w=int(tw),
                        h=int(th),
                        features=matching_features(scaled_template),
                    )
                )

    result = prepared, missing
    _prepared_templates_cache[key] = result
    return result


def _match_template_worker(template, screenshot_features, screenshot_h, screenshot_w, config):
    if template.h > screenshot_h or template.w > screenshot_w:
        return None
    label = template.label
    threshold = config.template_thresholds.get(label, config.threshold)
    result = combined_match_score(screenshot_features, template.features, config)
    return label, threshold, result, template.w, template.h


def determine_best_scale(screenshot_img, config):
    """Finds the best template scale factor dynamically by scanning a range of scales."""
    representative_paths = []
    for item in config.items:
        paths = template_paths(item, 1, config)
        if paths:
            representative_paths.append(paths[0])
            if len(representative_paths) >= 3:
                break

    if not representative_paths:
        return 1.0, 0.0

    templates = []
    for path in representative_paths:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            templates.append(img)

    if not templates:
        return 1.0, 0.0

    gray_screenshot = cv2.cvtColor(screenshot_img, cv2.COLOR_BGR2GRAY)
    gray_screenshot = cv2.GaussianBlur(gray_screenshot, (3, 3), 0)
    screen_h, screen_w = gray_screenshot.shape[:2]

    # Helper function to evaluate score of a single scale
    def evaluate_scale(scale):
        scores = []
        for template in templates:
            th, tw = template.shape[:2]
            width = max(3, int(round(tw * scale)))
            height = max(3, int(round(th * scale)))

            if height > screen_h or width > screen_w:
                continue

            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            resized = cv2.resize(template, (width, height), interpolation=interpolation)

            res = cv2.matchTemplate(gray_screenshot, resized, cv2.TM_CCOEFF_NORMED)
            if res.size > 0:
                scores.append(float(np.max(res)))

        if scores:
            scores.sort(reverse=True)
            top_n = scores[:min(len(scores), 3)]
            return sum(top_n) / len(top_n)
        return 0.0

    key_str = f"{screen_h}x{screen_w}"
    cached_scale = _scale_cache.get(key_str)

    if cached_scale is not None:
        best_val = evaluate_scale(cached_scale)
        if best_val >= 0.50:
            return cached_scale, best_val

    # Coarse search range: 0.5 to 1.5 with 0.1 step
    coarse_scales = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
    best_coarse_scale = 1.0
    best_coarse_val = -1.0

    for scale in coarse_scales:
        avg_score = evaluate_scale(scale)
        if avg_score > best_coarse_val:
            best_coarse_val = avg_score
            best_coarse_scale = scale

    # Fine search around best_coarse_scale
    fine_scales = [best_coarse_scale + i * 0.02 for i in range(-4, 5)]
    # Filter to valid ranges
    fine_scales = [s for s in fine_scales if 0.4 <= s <= 1.6]

    best_scale = best_coarse_scale
    best_val = best_coarse_val

    for scale in fine_scales:
        if scale == best_coarse_scale:
            continue
        avg_score = evaluate_scale(scale)
        if avg_score > best_val:
            best_val = avg_score
            best_scale = scale

    _scale_cache[key_str] = best_scale

    return best_scale, best_val


def detect_all_items(
    screenshot_img,
    config,
    offset=(0, 0),
):
    from concurrent.futures import ThreadPoolExecutor
    import os

    original_scales = config.template_scales
    try:
        if len(original_scales) == 1:
            detected_scale, _ = determine_best_scale(screenshot_img, config)
            config.template_scales = (detected_scale,)

        screenshot_features = matching_features(screenshot_img)
        screenshot_h, screenshot_w = screenshot_img.shape[:2]
        detections = []
        offset_x, offset_y = offset
        prepared_templates, missing_templates = load_prepared_templates(config)

        for item, level, _ in missing_templates:
            print(
                "Skipping missing templates: "
                f"{config.template_dir / f'{item}{level}.png'} or "
                f"{config.template_dir / f'{item}{level}_<variant>.png'}"
            )

        num_workers = min(8, os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [
                executor.submit(_match_template_worker, template, screenshot_features, screenshot_h, screenshot_w, config)
                for template in prepared_templates
            ]

            for future in futures:
                res_one = future.result()
                if res_one is None:
                    continue

                label, threshold, result, tw, th = res_one

                local_max = result == cv2.dilate(
                    result,
                    np.ones((config.local_max_kernel, config.local_max_kernel), np.uint8),
                )
                ys, xs = np.where((result >= threshold) & local_max)

                for x, y in zip(xs, ys):
                    detections.append(
                        Detection(
                            label=label,
                            x=int(x + offset_x),
                            y=int(y + offset_y),
                            w=tw,
                            h=th,
                            score=float(result[y, x]),
                            grid_anchor_y_factor=config.grid_anchor_y_factor,
                        )
                    )

        detections = deduplicate_detections(detections, config)

        return detections
    finally:
        config.template_scales = original_scales


def find_game_region(screenshot_img, config):
    """Returns the largest dense, colorful viewport as an (x, y, w, h) box."""
    screen_h, screen_w = screenshot_img.shape[:2]
    hsv = cv2.cvtColor(screenshot_img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        (0, config.game_min_saturation, config.game_min_brightness),
        (179, 255, 255),
    )
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    min_area = screen_h * screen_w * config.game_min_screen_area
    candidates = []

    for x, y, w, h, area in stats[1:]:
        if area >= min_area and area / (w * h) >= config.game_min_fill_ratio:
            candidates.append((int(area), int(x), int(y), int(w), int(h)))

    if not candidates:
        return None

    _, x, y, w, h = max(candidates)
    left = max(0, x - config.game_crop_padding)
    top = max(0, y - config.game_crop_padding)
    right = min(screen_w, x + w + config.game_crop_padding)
    bottom = min(screen_h, y + h + config.game_crop_padding)
    return left, top, right - left, bottom - top


def capture_game_bgr(config):
    """Captures the desktop once, then keeps only the detected game viewport."""
    win = focus_game_window(config) if config.window_title else None

    with mss.mss() as sct:
        if win is not None:
            monitor = {
                "top": win.top,
                "left": win.left,
                "width": win.width,
                "height": win.height,
            }
            fallback = (0, 0, win.width, win.height)
        else:
            monitor = sct.monitors[1]
            fallback = None

        sct_img = sct.grab(monitor)
        full_image = cv2.cvtColor(np.array(sct_img), cv2.COLOR_BGRA2BGR)
        region = find_game_region(full_image, config) or fallback
        if region is None:
            raise RuntimeError("Game viewport not found. Keep the game visible and retry.")

        x, y, w, h = region
        cropped_image = full_image[y : y + h, x : x + w]
        actual_offset = (monitor["left"] + x, monitor["top"] + y)
        return cropped_image, actual_offset
