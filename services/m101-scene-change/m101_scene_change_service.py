#!/usr/bin/env python3
import argparse
import json
import logging
import signal
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests


APP_DIR = Path("/oem/smart-gw/m101_scene_change")
BASELINE_DIR = APP_DIR / "baselines"
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
CHANNEL_CACHE_PATH = APP_DIR / "channels.cache.json"
LOG_PATH = APP_DIR / "m101_scene_change.log"
SNAP_DB_PATH = Path("/oem/smart-gw/db/snap.db")
SNAP_IMAGE_DIR = Path("/userdata/mpp/disk")
SNAP_SOURCE_IMAGE_DIR = Path("/userdata/mpp/sdisk")
PIC_COUNTER_PATH = APP_DIR / "picture_counters.json"
CHANNEL_API = "http://127.0.0.1/api/v1/system/channels/mag"

MODEL_ID = 101
CLASS_ID = 99001
CLASS_NAME = "画面变化"
STOP = False


DEFAULT_CONFIG = {
    "enabled": True,
    "channels": list(range(1, 17)),
    "monitor_regions_by_channel": {},
    "interval_seconds": 300,
    "confirm_delay_seconds": 8,
    "consecutive_alarm_count": 1,
    "alarm_cooldown_seconds": 1800,
    "capture_attempts": 120,
    "capture_delay_seconds": 0.05,
    "change_threshold": 0.62,
    "block_threshold": 0.28,
    "warning_threshold": 0.45,
    "warning_block_threshold": 0.20,
    "update_baseline_after_scene_alarm": True,
    "update_baseline_after_quality_alarm": False,
    "write_warning_to_history": False,
    "bad_frame_filter_enabled": True,
    "bad_frame_alarm": True,
    "bad_frame_solid_ratio_threshold": 0.50,
    "bad_frame_low_detail_block_ratio": 0.55,
    "suppress_same_view_motion": True,
    "global_motion_min_matches": 14,
    "global_motion_min_inlier_ratio": 0.35,
    "global_motion_raw_same_view_min_matches": 40,
    "global_motion_shift_pixels": 8.0,
    "global_motion_rotation_degrees": 2.5,
    "global_motion_scale_delta": 0.04,
    "stable_roi_confirm_enabled": True,
    "stable_roi_regions": [
        [0.0, 0.0, 1.0, 0.28],
        [0.0, 0.25, 0.18, 1.0],
        [0.82, 0.25, 1.0, 1.0],
    ],
    "stable_roi_confirm_mode": "motion",
    "stable_roi_min_regions": 2,
    "stable_roi_require_top_region": True,
    "stable_roi_motion_min_regions": 1,
    "stable_roi_motion_min_matches": 8,
    "stable_roi_motion_min_inlier_ratio": 0.45,
    "stable_roi_motion_shift_pixels": 6.0,
    "stable_roi_motion_match_distance": 72,
    "stable_roi_mean_diff_threshold": 35.0,
    "stable_roi_block_threshold": 0.75,
    "stable_roi_block_diff_threshold": 18.0,
    "stable_roi_edge_diff_threshold": 0.08,
    "alarm_output_mode": "smart_gw",
    "direct_db_fallback": True,
    "smart_gw_mqtt_host": "127.0.0.1",
    "smart_gw_mqtt_port": 1883,
    "smart_gw_mqtt_topic": "/smart_gw/cmd",
    "smart_gw_mqtt_password": "",
}


def handle_signal(_signum, _frame):
    global STOP
    STOP = True


def ensure_dirs():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    SNAP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    SNAP_SOURCE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(verbose=False):
    ensure_dirs()
    handlers = [logging.FileHandler(str(LOG_PATH), encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def load_config():
    ensure_dirs()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw)
    return cfg


def load_state():
    if not STATE_PATH.exists():
        return {"last_alarm": {}, "last_result": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("failed to read state, recreating")
        return {"last_alarm": {}, "last_result": {}}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def fetch_channels(cfg):
    enabled = set(int(x) for x in cfg.get("channels", []))
    try:
        resp = requests.get(CHANNEL_API, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("result") or []
        CHANNEL_CACHE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("channel api failed, falling back to cache")
        items = json.loads(CHANNEL_CACHE_PATH.read_text(encoding="utf-8")) if CHANNEL_CACHE_PATH.exists() else []

    channels = []
    for item in items:
        ch_no = int(item.get("chNo") or 0)
        url = item.get("rtspURL") or ""
        if ch_no not in enabled or not url:
            continue
        if int(item.get("switch") or 0) != 1:
            continue
        if int(item.get("status") or 0) != 1:
            continue
        channels.append(
            {
                "ch_no": ch_no,
                "location": item.get("location") or "",
                "desc": item.get("desc") or "",
                "ip": item.get("ip") or item.get("ipAddr") or "",
                "sn": item.get("sn") or "",
                "sn32": item.get("sn32") or "",
                "url": url,
            }
        )
    return sorted(channels, key=lambda x: x["ch_no"])


def capture_frame(url, attempts=120, delay=0.05):
    cap = cv2.VideoCapture(url)
    frame = None
    for _ in range(int(attempts)):
        ok, img = cap.read()
        if ok and img is not None and img.size:
            frame = img
            break
        time.sleep(float(delay))
    cap.release()
    if frame is None:
        raise RuntimeError("failed to capture frame")
    return frame


def preprocess(frame, size=(320, 180)):
    small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray_blur = cv2.GaussianBlur(gray, (5, 5), 0)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    edges = cv2.Canny(gray_blur, 60, 160)
    return small, gray_blur, hsv, edges


def feature(frame):
    _small, gray, hsv, edges = preprocess(frame)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return {
        "gray": gray,
        "edges": edges,
        "hist": hist,
        "brightness": float(gray.mean()),
        "dark_ratio": float((gray < 25).mean()),
        "bright_ratio": float((gray > 235).mean()),
        "blur_lap_var": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    }


def monitor_region_for_channel(cfg, ch_no):
    regions = cfg.get("monitor_regions_by_channel") or {}
    raw = regions.get(str(int(ch_no)))
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(value) for value in raw]
    except (TypeError, ValueError):
        logging.warning("ch%s invalid monitor region=%r", ch_no, raw)
        return None
    x1, y1 = max(0.0, min(x1, 1.0)), max(0.0, min(y1, 1.0))
    x2, y2 = max(0.0, min(x2, 1.0)), max(0.0, min(y2, 1.0))
    if x2 - x1 < 0.02 or y2 - y1 < 0.02:
        logging.warning("ch%s monitor region is too small=%r", ch_no, raw)
        return None
    return [x1, y1, x2, y2]


def crop_monitor_region(frame, region):
    if not region:
        return frame
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = region
    left, top = int(round(x1 * w)), int(round(y1 * h))
    right, bottom = int(round(x2 * w)), int(round(y2 * h))
    left, top = max(0, min(left, w - 1)), max(0, min(top, h - 1))
    right, bottom = max(left + 1, min(right, w)), max(top + 1, min(bottom, h))
    return frame[top:bottom, left:right]


def detect_bad_frame(frame, cfg):
    if not cfg.get("bad_frame_filter_enabled", True):
        return [], {}

    small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    quant = (small // 32).reshape(-1, 3)
    _, counts = np.unique(quant, axis=0, return_counts=True)
    dominant_color_ratio = float(counts.max() / quant.shape[0]) if len(counts) else 0.0

    h, w = gray.shape
    low_detail_blocks = 0
    total_blocks = 8 * 6
    for y in range(6):
        for x in range(8):
            x1, x2 = int(x * w / 8), int((x + 1) * w / 8)
            y1, y2 = int(y * h / 6), int((y + 1) * h / 6)
            block = gray[y1:y2, x1:x2]
            if float(block.std()) < 3.0:
                low_detail_blocks += 1
    low_detail_block_ratio = low_detail_blocks / total_blocks

    flags = []
    if dominant_color_ratio >= float(cfg["bad_frame_solid_ratio_threshold"]):
        flags.append("bad_frame_solid_color")
    if (
        low_detail_block_ratio >= float(cfg["bad_frame_low_detail_block_ratio"])
        and cv2.Laplacian(gray, cv2.CV_64F).var() < 12
    ):
        flags.append("bad_frame_blocky_or_frozen")

    metrics = {
        "dominant_color_ratio": round(dominant_color_ratio, 4),
        "low_detail_block_ratio": round(low_detail_block_ratio, 4),
    }
    return flags, metrics


def estimate_global_motion(base_frame, current_frame, cfg):
    result = {
        "match_count": 0,
        "inlier_ratio": 0.0,
        "raw_median_shift_px": None,
        "median_shift_px": None,
        "rotation_degrees": None,
        "scale_delta": None,
        "same_view_motion": False,
        "global_motion": False,
    }
    try:
        base_small = cv2.resize(base_frame, (320, 180), interpolation=cv2.INTER_AREA)
        cur_small = cv2.resize(current_frame, (320, 180), interpolation=cv2.INTER_AREA)
        base_gray = cv2.cvtColor(base_small, cv2.COLOR_BGR2GRAY)
        cur_gray = cv2.cvtColor(cur_small, cv2.COLOR_BGR2GRAY)
        orb = cv2.ORB_create(nfeatures=500)
        kp1, des1 = orb.detectAndCompute(base_gray, None)
        kp2, des2 = orb.detectAndCompute(cur_gray, None)
        if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
            return result

        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = sorted(matcher.match(des1, des2), key=lambda m: m.distance)
        good = [m for m in matches if m.distance <= 72][:120]
        result["match_count"] = len(good)
        if len(good) < int(cfg["global_motion_min_matches"]):
            return result

        pts1 = np.float32([kp1[m.queryIdx].pt for m in good])
        pts2 = np.float32([kp2[m.trainIdx].pt for m in good])
        raw_shift = float(np.median(np.linalg.norm(pts2 - pts1, axis=1)))
        result["raw_median_shift_px"] = round(raw_shift, 2)
        raw_same_view = (
            len(good) >= int(cfg["global_motion_raw_same_view_min_matches"])
            and raw_shift < float(cfg["global_motion_shift_pixels"])
        )
        matrix, inliers = cv2.estimateAffinePartial2D(
            pts1, pts2, method=cv2.RANSAC, ransacReprojThreshold=4.0
        )
        if matrix is None or inliers is None:
            result["same_view_motion"] = raw_same_view
            return result

        mask = inliers.ravel().astype(bool)
        inlier_count = int(mask.sum())
        if inlier_count < int(cfg["global_motion_min_matches"]):
            result["same_view_motion"] = raw_same_view
            return result

        result["inlier_ratio"] = round(inlier_count / len(good), 4)
        shifts = np.linalg.norm(pts2[mask] - pts1[mask], axis=1)
        median_shift = float(np.median(shifts))
        rotation = float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))
        scale = float(np.sqrt(matrix[0, 0] ** 2 + matrix[1, 0] ** 2))
        scale_delta = abs(scale - 1.0)

        result["median_shift_px"] = round(median_shift, 2)
        result["rotation_degrees"] = round(rotation, 2)
        result["scale_delta"] = round(scale_delta, 4)
        result["global_motion"] = (
            median_shift >= float(cfg["global_motion_shift_pixels"])
            or abs(rotation) >= float(cfg["global_motion_rotation_degrees"])
            or scale_delta >= float(cfg["global_motion_scale_delta"])
        )
        result["same_view_motion"] = (
            (
                result["inlier_ratio"] >= float(cfg["global_motion_min_inlier_ratio"])
                or raw_same_view
            )
            and not result["global_motion"]
        )
    except Exception:
        logging.exception("global motion estimation failed")
    return result


def block_change_ratio(g1, g2, grid=(8, 6), threshold=18.0):
    h, w = g1.shape
    gx, gy = grid
    changed = 0
    total = gx * gy
    rects = []
    for y in range(gy):
        for x in range(gx):
            x1, x2 = int(x * w / gx), int((x + 1) * w / gx)
            y1, y2 = int(y * h / gy), int((y + 1) * h / gy)
            score = float(np.mean(np.abs(g1[y1:y2, x1:x2].astype(np.int16) - g2[y1:y2, x1:x2].astype(np.int16))))
            if score >= threshold:
                changed += 1
                rects.append([x1, y1, x2, y2, round(score, 2)])
    return changed / total, rects



def estimate_roi_motion(g1, g2, cfg):
    result = {
        "match_count": 0,
        "inlier_ratio": 0.0,
        "raw_median_shift_px": None,
        "median_shift_px": None,
        "motion_changed": False,
    }
    try:
        if g1.size == 0 or g2.size == 0:
            return result
        a = cv2.equalizeHist(g1)
        b = cv2.equalizeHist(g2)
        orb = cv2.ORB_create(nfeatures=int(cfg.get("stable_roi_motion_nfeatures", 240)))
        kp1, des1 = orb.detectAndCompute(a, None)
        kp2, des2 = orb.detectAndCompute(b, None)
        if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
            return result

        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = sorted(matcher.match(des1, des2), key=lambda m: m.distance)
        max_distance = float(cfg.get("stable_roi_motion_match_distance", 72))
        good = [m for m in matches if m.distance <= max_distance][:80]
        result["match_count"] = len(good)
        min_matches = int(cfg.get("stable_roi_motion_min_matches", 8))
        if len(good) < min_matches:
            return result

        pts1 = np.float32([kp1[m.queryIdx].pt for m in good])
        pts2 = np.float32([kp2[m.trainIdx].pt for m in good])
        raw_shift = float(np.median(np.linalg.norm(pts2 - pts1, axis=1)))
        result["raw_median_shift_px"] = round(raw_shift, 2)
        matrix, inliers = cv2.estimateAffinePartial2D(
            pts1, pts2, method=cv2.RANSAC, ransacReprojThreshold=3.0
        )
        if matrix is None or inliers is None:
            return result

        mask = inliers.ravel().astype(bool)
        inlier_count = int(mask.sum())
        if inlier_count < min_matches:
            return result
        inlier_ratio = inlier_count / len(good)
        shifts = np.linalg.norm(pts2[mask] - pts1[mask], axis=1)
        median_shift = float(np.median(shifts))
        result["inlier_ratio"] = round(inlier_ratio, 4)
        result["median_shift_px"] = round(median_shift, 2)
        result["motion_changed"] = (
            inlier_ratio >= float(cfg.get("stable_roi_motion_min_inlier_ratio", 0.45))
            and median_shift >= float(cfg.get("stable_roi_motion_shift_pixels", 6.0))
        )
    except Exception:
        logging.exception("stable roi motion estimation failed")
    return result



def stable_roi_metrics(g1, g2, cfg):
    default_regions = [
        [0.0, 0.0, 1.0, 0.28],
        [0.0, 0.25, 0.18, 1.0],
        [0.82, 0.25, 1.0, 1.0],
    ]
    regions = cfg.get("stable_roi_regions") or default_regions
    mean_threshold = float(cfg.get("stable_roi_mean_diff_threshold", 14.0))
    block_threshold = float(cfg.get("stable_roi_block_threshold", 0.35))
    block_diff_threshold = float(cfg.get("stable_roi_block_diff_threshold", 18.0))
    edge_diff_threshold = float(cfg.get("stable_roi_edge_diff_threshold", 0.08))
    confirm_mode = str(cfg.get("stable_roi_confirm_mode", "motion")).lower()
    min_regions = int(cfg.get("stable_roi_min_regions", 2))
    require_top = bool(cfg.get("stable_roi_require_top_region", True))
    motion_min_regions = int(cfg.get("stable_roi_motion_min_regions", 1))
    h, w = g1.shape
    details = []
    changed = 0
    motion_changed = 0
    for idx, region in enumerate(regions):
        try:
            x1f, y1f, x2f, y2f = [float(v) for v in region]
        except Exception:
            continue
        x1 = max(0, min(w - 1, int(round(x1f * w))))
        x2 = max(x1 + 1, min(w, int(round(x2f * w))))
        y1 = max(0, min(h - 1, int(round(y1f * h))))
        y2 = max(y1 + 1, min(h, int(round(y2f * h))))
        r1 = g1[y1:y2, x1:x2]
        r2 = g2[y1:y2, x1:x2]
        if r1.size == 0 or r2.size == 0:
            continue
        mean_diff = float(np.mean(np.abs(r1.astype(np.int16) - r2.astype(np.int16))))
        block_ratio, _rects = block_change_ratio(r1, r2, grid=(4, 3), threshold=block_diff_threshold)
        edges1 = cv2.Canny(r1, 60, 160)
        edges2 = cv2.Canny(r2, 60, 160)
        edge_diff = float(np.mean(np.abs(edges1.astype(np.int16) - edges2.astype(np.int16))) / 255.0)
        motion = estimate_roi_motion(r1, r2, cfg)
        pixel_changed = (
            mean_diff >= mean_threshold
            and block_ratio >= block_threshold
            and edge_diff >= edge_diff_threshold
        )
        if confirm_mode == "motion":
            region_changed = bool(motion.get("motion_changed"))
        elif confirm_mode == "structural":
            region_changed = pixel_changed
        else:
            region_changed = bool(motion.get("motion_changed")) or pixel_changed
        if region_changed:
            changed += 1
        if motion.get("motion_changed"):
            motion_changed += 1
        details.append({
            "idx": idx,
            "rect": [round(x1 / w, 3), round(y1 / h, 3), round(x2 / w, 3), round(y2 / h, 3)],
            "mean_diff": round(mean_diff, 2),
            "block_ratio": round(block_ratio, 4),
            "edge_diff": round(edge_diff, 4),
            "pixel_changed": pixel_changed,
            "motion": motion,
            "changed": region_changed,
        })
    top_changed = bool(details and details[0].get("changed"))
    top_motion_changed = bool(details and details[0].get("motion", {}).get("motion_changed"))
    if confirm_mode == "motion":
        stable_confirmed = (
            motion_changed >= motion_min_regions
            and (not require_top or top_motion_changed)
        )
    else:
        stable_confirmed = (
            changed >= min_regions
            and (not require_top or top_changed)
        )
    return {
        "enabled": bool(cfg.get("stable_roi_confirm_enabled", True)),
        "confirm_mode": confirm_mode,
        "changed_regions": changed,
        "motion_changed_regions": motion_changed,
        "total_regions": len(details),
        "min_regions": min_regions,
        "motion_min_regions": motion_min_regions,
        "top_required": require_top,
        "top_changed": top_changed,
        "top_motion_changed": top_motion_changed,
        "stable_confirmed": stable_confirmed,
        "details": details,
    }


def compare(base_frame, current_frame, cfg, monitor_region=None):
    bad_frame_flags, bad_frame_metrics = detect_bad_frame(current_frame, cfg)
    base_compare = crop_monitor_region(base_frame, monitor_region)
    current_compare = crop_monitor_region(current_frame, monitor_region)
    b = feature(base_compare)
    c = feature(current_compare)
    global_motion = estimate_global_motion(base_frame, current_frame, cfg)
    gray_diff = float(np.mean(np.abs(b["gray"].astype(np.int16) - c["gray"].astype(np.int16))))
    edge_diff = float(np.mean(np.abs(b["edges"].astype(np.int16) - c["edges"].astype(np.int16))) / 255.0)
    hist_diff = float(cv2.compareHist(b["hist"], c["hist"], cv2.HISTCMP_BHATTACHARYYA))
    block_ratio, rects = block_change_ratio(b["gray"], c["gray"])
    brightness_delta = abs(c["brightness"] - b["brightness"])

    quality_flags = []
    if c["dark_ratio"] > 0.75:
        quality_flags.append("dark_or_blocked")
    if c["bright_ratio"] > 0.75:
        quality_flags.append("white_or_overexposed")
    if c["blur_lap_var"] < 18:
        quality_flags.append("blurred")
    if brightness_delta > float(cfg.get("quality_brightness_shift_threshold", 55)):
        quality_flags.append("large_brightness_shift")
    if cfg.get("decode_artifact_filter_enabled", True):
        raw_shift = global_motion.get("raw_median_shift_px")
        if (
            int(global_motion.get("match_count") or 0) >= int(cfg.get("decode_artifact_min_matches", 50))
            and float(global_motion.get("inlier_ratio") or 0.0) <= float(cfg.get("decode_artifact_max_inlier_ratio", 0.01))
            and raw_shift is not None
            and float(raw_shift) >= float(cfg.get("decode_artifact_min_raw_shift", 80.0))
            and brightness_delta >= float(cfg.get("decode_artifact_min_brightness_delta", 25.0))
        ):
            quality_flags.append("decode_artifact_or_stream_cut")
    quality_flags.extend(bad_frame_flags)

    change_score = (
        min(gray_diff / 45.0, 1.0) * 0.35
        + min(hist_diff / 0.55, 1.0) * 0.25
        + min(block_ratio / 0.45, 1.0) * 0.30
        + min(edge_diff / 0.35, 1.0) * 0.10
    )

    status = "normal"
    event = "none"
    scene_candidate = change_score >= float(cfg["change_threshold"]) and block_ratio >= float(cfg["block_threshold"])
    warning_candidate = (
        change_score >= float(cfg["warning_threshold"])
        and block_ratio >= float(cfg["warning_block_threshold"])
    )
    stable_roi = stable_roi_metrics(b["gray"], c["gray"], cfg)
    same_view_suppressed = False
    stable_roi_suppressed = False
    if quality_flags and not cfg.get("bad_frame_alarm", True):
        event = f"{quality_flags[0]}_ignored"
    elif quality_flags:
        status = "alarm"
        event = quality_flags[0]
    elif scene_candidate and cfg.get("suppress_same_view_motion", True) and global_motion.get("same_view_motion"):
        event = "same_view_motion_ignored"
        same_view_suppressed = True
    elif scene_candidate and cfg.get("stable_roi_confirm_enabled", True) and not stable_roi.get("stable_confirmed", False):
        event = "stable_roi_ignored"
        stable_roi_suppressed = True
    elif scene_candidate:
        status = "alarm"
        event = "scene_changed"
    elif warning_candidate and not (
        cfg.get("suppress_same_view_motion", True) and global_motion.get("same_view_motion")
    ):
        status = "warning"
        event = "possible_scene_changed"

    return {
        "status": status,
        "event": event,
        "change_score": round(change_score, 4),
        "gray_diff": round(gray_diff, 2),
        "hist_diff": round(hist_diff, 4),
        "edge_diff": round(edge_diff, 4),
        "block_change_ratio": round(block_ratio, 4),
        "brightness": round(c["brightness"], 2),
        "brightness_delta": round(brightness_delta, 2),
        "dark_ratio": round(c["dark_ratio"], 4),
        "bright_ratio": round(c["bright_ratio"], 4),
        "blur_lap_var": round(c["blur_lap_var"], 2),
        "quality_flags": quality_flags,
        "bad_frame_flags": bad_frame_flags,
        "bad_frame_metrics": bad_frame_metrics,
        "global_motion": global_motion,
        "same_view_suppressed": same_view_suppressed,
        "stable_roi_suppressed": stable_roi_suppressed,
        "stable_roi": stable_roi,
        "changed_blocks": rects[:30],
        "monitor_region": monitor_region,
    }


def baseline_path(ch_no):
    return BASELINE_DIR / f"ch{int(ch_no)}.jpg"


def load_baseline(ch_no):
    path = baseline_path(ch_no)
    if not path.exists():
        return None
    img = cv2.imread(str(path))
    return img if img is not None and img.size else None


def save_baseline(ch_no, frame):
    cv2.imwrite(str(baseline_path(ch_no)), frame)


def draw_overlay(current_frame, result):
    h, w = current_frame.shape[:2]
    canvas = current_frame.copy()
    color = (0, 0, 255) if result["status"] == "alarm" else (0, 200, 255)
    region = result.get("monitor_region")
    if region:
        rx1, ry1, rx2, ry2 = region
        region_left, region_top = int(rx1 * w), int(ry1 * h)
        region_width, region_height = int((rx2 - rx1) * w), int((ry2 - ry1) * h)
        cv2.rectangle(
            canvas,
            (region_left, region_top),
            (region_left + region_width, region_top + region_height),
            (255, 180, 0),
            2,
        )
    else:
        region_left, region_top = 0, 0
        region_width, region_height = w, h
    for x1, y1, x2, y2, _score in result.get("changed_blocks", []):
        sx1 = region_left + int(x1 * region_width / 320)
        sy1 = region_top + int(y1 * region_height / 180)
        sx2 = region_left + int(x2 * region_width / 320)
        sy2 = region_top + int(y2 * region_height / 180)
        cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), color, 2)
    text = f"m101 scene_change score={result['change_score']} blocks={result['block_change_ratio']} {result['event']}"
    cv2.rectangle(canvas, (8, 8), (min(w - 8, 8 + len(text) * 10), 44), (0, 0, 0), -1)
    cv2.putText(canvas, text, (14, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.58, color, 2)
    return canvas


def next_flag_id(con, ch_no):
    row = con.execute(
        "select maxID from ch_g_max_ids where chNo=? and geid=?",
        (ch_no, MODEL_ID),
    ).fetchone()
    if row:
        flag_id = int(row[0]) + 1
        con.execute(
            "update ch_g_max_ids set maxID=? where chNo=? and geid=?",
            (flag_id, ch_no, MODEL_ID),
        )
    else:
        flag_id = 1
        con.execute(
            "insert into ch_g_max_ids (chNo, geid, maxID) values (?, ?, ?)",
            (ch_no, MODEL_ID, flag_id),
        )
    return flag_id


def load_picture_counters():
    if not PIC_COUNTER_PATH.exists():
        return {}
    try:
        return json.loads(PIC_COUNTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("failed to read picture counters, recreating")
        return {}


def next_picture_seq(ch_no):
    counters = load_picture_counters()
    key = str(ch_no)
    seq = int(counters.get(key, 0)) + 1
    counters[key] = seq
    tmp = PIC_COUNTER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(counters, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PIC_COUNTER_PATH)
    return seq


def build_nn_output(result):
    return [
        {
            "conf": float(result["change_score"]),
            "gcid": CLASS_ID,
            "aid": 0,
            "cid": CLASS_ID,
            "class_name": CLASS_NAME,
            "x1": 0.0,
            "y1": 0.0,
            "x2": 1.0,
            "y2": 1.0,
        }
    ]


def build_smart_gw_payload(ch_no, channel, frame, result, seq, pic_name, spic_name):
    h, w = frame.shape[:2]
    return {
        "cmd": "ch_detect_rsp",
        "param": {
            "chid": int(ch_no),
            "ncid": 0,
            "ip": channel.get("ip") or "",
            "surl": channel.get("url") or "",
            "geid": MODEL_ID,
            "sn": channel.get("sn") or "",
            "sn32": channel.get("sn32") or "",
            "location": channel.get("location") or "",
            "width": int(w),
            "height": int(h),
            "nn_output": build_nn_output(result),
            "desc": channel.get("desc") or channel.get("location") or "",
            "seq": int(seq),
            "sdpath": SNAP_SOURCE_IMAGE_DIR.as_posix() + "/",
            "dpath": SNAP_IMAGE_DIR.as_posix() + "/",
            "sfname": spic_name,
            "fname": pic_name,
        },
    }


def publish_smart_gw_payload(cfg, payload):
    try:
        import paho.mqtt.publish as publish
    except Exception as exc:
        raise RuntimeError("paho-mqtt is required for smart_gw alarm output") from exc

    client_id = f"m101_{MODEL_ID}_{int(time.time())}"
    password = str(cfg.get("smart_gw_mqtt_password") or "")
    auth = {"username": client_id, "password": password} if password else None
    publish.single(
        str(cfg["smart_gw_mqtt_topic"]),
        payload=json.dumps(payload, ensure_ascii=False),
        qos=0,
        hostname=str(cfg["smart_gw_mqtt_host"]),
        port=int(cfg["smart_gw_mqtt_port"]),
        client_id=client_id,
        auth=auth,
    )


def write_alarm_to_smart_gw(ch_no, channel, frame, result, cfg):
    SNAP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    SNAP_SOURCE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    seq = next_picture_seq(ch_no)
    pic_name = f"ch{ch_no}_m{MODEL_ID}_{seq}.jpg"
    spic_name = f"s_{pic_name}"
    overlay = draw_overlay(frame, result)
    cv2.imwrite(str(SNAP_SOURCE_IMAGE_DIR / spic_name), frame)
    cv2.imwrite(str(SNAP_IMAGE_DIR / pic_name), overlay)
    payload = build_smart_gw_payload(ch_no, channel, frame, result, seq, pic_name, spic_name)
    publish_smart_gw_payload(cfg, payload)
    return {
        "seq": seq,
        "pic_name": pic_name,
        "spic_name": spic_name,
        "output": "smart_gw",
    }


def write_alarm_to_snap_db(ch_no, frame, result):
    SNAP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(SNAP_DB_PATH), timeout=10) as con:
        con.execute("begin immediate")
        flag_id = next_flag_id(con, ch_no)
        pic_name = f"ch{ch_no}_m{MODEL_ID}_{flag_id}.jpg"
        spic_name = f"s_ch{ch_no}_m{MODEL_ID}_{flag_id}.jpg"

        overlay = draw_overlay(frame, result)
        cv2.imwrite(str(SNAP_IMAGE_DIR / pic_name), overlay)
        thumb = cv2.resize(overlay, (320, int(overlay.shape[0] * 320 / overlay.shape[1])), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(SNAP_IMAGE_DIR / spic_name), thumb)

        now = int(time.time())
        now_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        detects = {
            "detects": [
                {
                    "conf": float(result["change_score"]),
                    "class": CLASS_ID,
                    "gcid": CLASS_ID,
                    "aid": 0,
                    "cid": CLASS_ID,
                    "class_name": CLASS_NAME,
                    "event": result["event"],
                    "x1": 0.0,
                    "y1": 0.0,
                    "x2": 1.0,
                    "y2": 1.0,
                }
            ],
            "m101": result,
        }
        con.execute(
            """
            insert into ch_g_imgs
              (chNo, flagID, geid, picName, spicName, cid, cname, detects, timeStamp, timeStampStr)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ch_no,
                flag_id,
                MODEL_ID,
                pic_name,
                spic_name,
                f",{CLASS_ID},",
                f",{CLASS_NAME},",
                json.dumps(detects, ensure_ascii=False, indent=2),
                now,
                now_str,
            ),
        )
        con.commit()
    return {
        "flag_id": flag_id,
        "pic_name": pic_name,
        "spic_name": spic_name,
        "timestamp": now,
        "output": "snap_db",
    }


def write_alarm(ch_no, channel, frame, result, cfg):
    mode = str(cfg.get("alarm_output_mode") or "smart_gw")
    if mode == "snap_db":
        return write_alarm_to_snap_db(ch_no, frame, result)
    try:
        return write_alarm_to_smart_gw(ch_no, channel, frame, result, cfg)
    except Exception as exc:
        if not cfg.get("direct_db_fallback", True):
            raise
        logging.exception("smart_gw alarm output failed, falling back to snap_db: %s", exc)
        return write_alarm_to_snap_db(ch_no, frame, result)


def should_write_alarm(state, ch_no, cfg):
    last_alarm = float(state.get("last_alarm", {}).get(str(ch_no), 0))
    return time.time() - last_alarm >= float(cfg["alarm_cooldown_seconds"])


def reset_consecutive_alarm(state, ch_no):
    state.setdefault("consecutive_alarm", {})[str(int(ch_no))] = {
        "count": 0,
        "last_time": 0,
    }


def record_consecutive_alarm(state, ch_no, cfg):
    key = str(int(ch_no))
    now = time.time()
    record = state.setdefault("consecutive_alarm", {}).get(key) or {}
    count = int(record.get("count") or 0)
    last_time = float(record.get("last_time") or 0)
    max_gap = max(120.0, float(cfg.get("interval_seconds", 300)) * 2.5)
    if last_time <= 0 or now - last_time > max_gap:
        count = 0
    count += 1
    state["consecutive_alarm"][key] = {
        "count": count,
        "last_time": now,
    }
    return count


def process_channel(channel, cfg, state, dry_run=False):
    ch_no = int(channel["ch_no"])
    url = channel["url"]
    frame = capture_frame(url, cfg["capture_attempts"], cfg["capture_delay_seconds"])
    base = load_baseline(ch_no)
    if base is None:
        bad_frame_flags, bad_frame_metrics = detect_bad_frame(frame, cfg)
        if bad_frame_flags:
            logging.info("ch%s baseline skipped bad frame flags=%s", ch_no, ",".join(bad_frame_flags))
            return {
                "ch": ch_no,
                "status": "bad_frame_baseline_skipped",
                "bad_frame_flags": bad_frame_flags,
                "bad_frame_metrics": bad_frame_metrics,
            }
        save_baseline(ch_no, frame)
        reset_consecutive_alarm(state, ch_no)
        logging.info("ch%s baseline initialized location=%s", ch_no, channel.get("location", ""))
        return {"ch": ch_no, "status": "baseline_initialized"}

    monitor_region = monitor_region_for_channel(cfg, ch_no)
    result = compare(base, frame, cfg, monitor_region=monitor_region)
    state.setdefault("last_result", {})[str(ch_no)] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "location": channel.get("location", ""),
        "result": result,
    }

    should_history = result["status"] == "alarm" or (result["status"] == "warning" and cfg.get("write_warning_to_history"))
    if not should_history:
        reset_consecutive_alarm(state, ch_no)
        logging.info("ch%s normal score=%s blocks=%s", ch_no, result["change_score"], result["block_change_ratio"])
        return {"ch": ch_no, **result}

    time.sleep(float(cfg["confirm_delay_seconds"]))
    confirm_frame = capture_frame(url, cfg["capture_attempts"], cfg["capture_delay_seconds"])
    confirm_result = compare(base, confirm_frame, cfg, monitor_region=monitor_region)
    if confirm_result["status"] == "normal":
        reset_consecutive_alarm(state, ch_no)
        logging.info("ch%s transient change ignored first=%s confirm=%s", ch_no, result["event"], confirm_result["status"])
        return {"ch": ch_no, "status": "transient_ignored", "first": result, "confirm": confirm_result}
    confirm_should_history = confirm_result["status"] == "alarm" or (
        confirm_result["status"] == "warning" and cfg.get("write_warning_to_history")
    )
    if not confirm_should_history:
        reset_consecutive_alarm(state, ch_no)
        logging.info(
            "ch%s confirm change ignored first=%s confirm=%s",
            ch_no,
            result["event"],
            confirm_result["event"],
        )
        return {"ch": ch_no, "status": "confirm_ignored", "first": result, "confirm": confirm_result}

    required_count = max(1, int(cfg.get("consecutive_alarm_count", 1)))
    consecutive_count = record_consecutive_alarm(state, ch_no, cfg)
    if consecutive_count < required_count:
        logging.warning(
            "ch%s consecutive change pending count=%s/%s event=%s score=%s",
            ch_no,
            consecutive_count,
            required_count,
            confirm_result["event"],
            confirm_result["change_score"],
        )
        return {
            "ch": ch_no,
            "status": "consecutive_pending",
            "count": consecutive_count,
            "required": required_count,
            "confirm": confirm_result,
        }

    if not should_write_alarm(state, ch_no, cfg):
        reset_consecutive_alarm(state, ch_no)
        logging.info("ch%s alarm suppressed by cooldown event=%s", ch_no, confirm_result["event"])
        return {"ch": ch_no, "status": "cooldown_suppressed", "confirm": confirm_result}

    alarm_info = None
    if not dry_run:
        alarm_info = write_alarm(ch_no, channel, confirm_frame, confirm_result, cfg)
        state.setdefault("last_alarm", {})[str(ch_no)] = time.time()
    reset_consecutive_alarm(state, ch_no)

    if confirm_result["event"] == "scene_changed" and cfg.get("update_baseline_after_scene_alarm"):
        save_baseline(ch_no, confirm_frame)
    elif confirm_result["event"] != "scene_changed" and cfg.get("update_baseline_after_quality_alarm"):
        save_baseline(ch_no, confirm_frame)

    pic_name = alarm_info["pic_name"] if alarm_info else None
    logging.warning("ch%s alarm event=%s score=%s pic=%s", ch_no, confirm_result["event"], confirm_result["change_score"], pic_name)
    return {
        "ch": ch_no,
        "status": "alarm_written" if alarm_info else "alarm_dry_run",
        "pic": pic_name,
        "output": alarm_info["output"] if alarm_info else None,
        "result": confirm_result,
    }


def run_once(cfg, dry_run=False):
    state = load_state()
    channels = fetch_channels(cfg)
    report = []
    for channel in channels:
        if STOP:
            break
        try:
            report.append(process_channel(channel, cfg, state, dry_run=dry_run))
        except Exception as exc:
            logging.exception("ch%s failed: %s", channel.get("ch_no"), exc)
            report.append({"ch": channel.get("ch_no"), "status": "failed", "error": str(exc)})
        save_state(state)
    return report


def run_service(cfg):
    logging.info("m101 scene change service started interval=%ss channels=%s", cfg["interval_seconds"], cfg["channels"])
    while not STOP:
        if not cfg.get("enabled", True):
            logging.info("service disabled by config")
            time.sleep(30)
            cfg = load_config()
            continue

        state = load_state()
        channels = fetch_channels(cfg)
        delay = max(1.0, float(cfg["interval_seconds"]) / max(len(channels), 1))
        for channel in channels:
            if STOP:
                break
            try:
                process_channel(channel, cfg, state, dry_run=False)
            except Exception as exc:
                logging.exception("ch%s failed: %s", channel.get("ch_no"), exc)
            save_state(state)
            slept = 0.0
            while slept < delay and not STOP:
                step = min(1.0, delay - slept)
                time.sleep(step)
                slept += step
        cfg = load_config()
    logging.info("m101 scene change service stopped")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    setup_logging(verbose=args.verbose or args.once)
    cfg = load_config()

    if args.once:
        print(json.dumps(run_once(cfg, dry_run=args.dry_run), ensure_ascii=False, indent=2))
        return
    run_service(cfg)


if __name__ == "__main__":
    main()
