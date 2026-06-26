import csv
from collections import Counter
from dataclasses import dataclass
import time
import cv2
import numpy as np
import mss
import pygetwindow as gw


def focus_game_window(config):
    """Finds the configured or Discord window, restores and activates it."""
    title = config.window_title
    if not title:
        # ponytail: auto-detect Discord by default if no window title is configured
        titles = [t for t in gw.getAllTitles() if "discord" in t.lower()]
        if titles:
            title = titles[0]
            config.window_title = title
            print(f"Auto-detected Discord window: '{title}'")
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
                time.sleep(0.5)  # ponytail: sleep to allow window activation and rendering
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
    template_name: str
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
                        template_name=template_path.name,
                        w=int(tw),
                        h=int(th),
                        features=matching_features(scaled_template),
                    )
                )

    return prepared, missing


def _init_diagnostics(threshold):
    return {
        "best_score": float("-inf"),
        "best_template": "",
        "best_width": 0,
        "best_height": 0,
        "best_x": 0,
        "best_y": 0,
        "threshold": threshold,
        "detected_count": 0,
    }


def _match_template_worker(template, screenshot_features, screenshot_h, screenshot_w, config):
    if template.h > screenshot_h or template.w > screenshot_w:
        return None
    label = template.label
    threshold = config.template_thresholds.get(label, config.threshold)
    result = combined_match_score(screenshot_features, template.features, config)
    return label, threshold, result, template.w, template.h, template.template_name


def detect_all_items(
    screenshot_img,
    config,
    diagnostics=None,
    offset=(0, 0),
):
    from concurrent.futures import ThreadPoolExecutor
    import os

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

    if diagnostics is not None:
        for _, _, label in configured_labels(config):
            diagnostics[label] = _init_diagnostics(
                config.template_thresholds.get(label, config.threshold)
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

            label, threshold, result, tw, th, template_name = res_one

            if diagnostics is not None:
                best_score = float(np.max(result))

                if best_score > diagnostics[label]["best_score"]:
                    best_y, best_x = np.unravel_index(
                        int(np.argmax(result)),
                        result.shape,
                    )
                    diagnostics[label].update(
                        {
                            "best_score": best_score,
                            "best_template": template_name,
                            "best_width": tw,
                            "best_height": th,
                            "best_x": int(best_x + offset_x),
                            "best_y": int(best_y + offset_y),
                        }
                    )

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

    if diagnostics is not None:
        detected_counts = Counter(detection.label for detection in detections)

        for label, values in diagnostics.items():
            values["detected_count"] = detected_counts[label]

    return detections


def save_detection_debug_images(
    screenshot_img,
    detections,
    config,
    diagnostics=None,
    image_offset=(0, 0),
    excluded_detections=None,
    suffix="",
):
    """Saves the captured board and an annotated copy for calibration."""
    config.detection_debug_dir.mkdir(parents=True, exist_ok=True)
    raw_path = config.detection_debug_dir / f"board{suffix}.png"
    annotated_path = config.detection_debug_dir / f"detections{suffix}.png"
    scores_path = config.detection_debug_dir / f"scores{suffix}.csv"
    annotated = screenshot_img.copy()
    offset_x, offset_y = image_offset
    for detection in detections:
        draw_x = detection.x - offset_x
        draw_y = detection.y - offset_y
        top_left = (draw_x, draw_y)
        bottom_right = (draw_x + detection.w, draw_y + detection.h)
        cv2.rectangle(annotated, top_left, bottom_right, (0, 255, 0), 1)
        cv2.putText(
            annotated,
            f"{detection.label} {detection.score:.2f}",
            (draw_x, max(10, draw_y - 3)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.32,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )

    for detection in excluded_detections or []:
        draw_x = detection.x - offset_x
        draw_y = detection.y - offset_y
        top_left = (draw_x, draw_y)
        bottom_right = (draw_x + detection.w, draw_y + detection.h)
        cv2.rectangle(annotated, top_left, bottom_right, (0, 165, 255), 2)
        cv2.putText(
            annotated,
            f"EXCLUDED {detection.label}",
            (draw_x, max(10, draw_y - 3)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.32,
            (0, 165, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.imwrite(str(raw_path), screenshot_img)
    cv2.imwrite(str(annotated_path), annotated)

    if diagnostics is not None:
        fieldnames = [
            "label",
            "best_score",
            "threshold",
            "detected_count",
            "best_template",
            "best_width",
            "best_height",
            "best_x",
            "best_y",
        ]

        with scores_path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()

            for label in sorted(diagnostics):
                values = diagnostics[label]
                writer.writerow(
                    {
                        "label": label,
                        "best_score": f"{values['best_score']:.4f}",
                        "threshold": f"{values['threshold']:.4f}",
                        "detected_count": values["detected_count"],
                        "best_template": values["best_template"],
                        "best_width": values["best_width"],
                        "best_height": values["best_height"],
                        "best_x": values["best_x"],
                        "best_y": values["best_y"],
                    }
                )

    return raw_path, annotated_path, scores_path


def save_merge_debug_image(screenshot_img, slots, merge_triggers, config, image_offset=(0, 0)):
    """Saves a debug image showing merge drags."""
    config.detection_debug_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.detection_debug_dir / "merges.png"
    canvas = screenshot_img.copy()
    offset_x, offset_y = image_offset

    for slot in slots:
        draw_x = slot.x - offset_x
        draw_y = slot.y - offset_y
        cv2.rectangle(canvas, (draw_x, draw_y), (draw_x + slot.w, draw_y + slot.h), (255, 0, 0), 1)
        cv2.putText(
            canvas, slot.label,
            (draw_x + 2, draw_y + 12),
            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1, cv2.LINE_AA,
        )

    for trigger in merge_triggers:
        src = slots[trigger["from_slot"]]
        dst = slots[trigger["to_slot"]]
        src_pt = (src.center[0] - offset_x, src.center[1] - offset_y)
        dst_pt = (dst.center[0] - offset_x, dst.center[1] - offset_y)
        cv2.arrowedLine(canvas, src_pt, dst_pt, (0, 255, 0), 2, tipLength=0.3, line_type=cv2.LINE_AA)

    cv2.imwrite(str(out_path), canvas)
    return out_path


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
