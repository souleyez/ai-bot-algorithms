#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np


def capture_frame(url, attempts=120, delay=0.05):
    cap = cv2.VideoCapture(url)
    frame = None
    for _ in range(attempts):
        ok, img = cap.read()
        if ok and img is not None and img.size:
            frame = img
            break
        time.sleep(delay)
    cap.release()
    if frame is None:
        raise RuntimeError(f"failed to capture frame from {url}")
    return frame


def preprocess(frame, size=(320, 180)):
    small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray_blur = cv2.GaussianBlur(gray, (5, 5), 0)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    edges = cv2.Canny(gray_blur, 60, 160)
    return small, gray_blur, hsv, edges


def feature(frame):
    small, gray, hsv, edges = preprocess(frame)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return {
        "small": small,
        "gray": gray,
        "edges": edges,
        "hist": hist,
        "brightness": float(gray.mean()),
        "dark_ratio": float((gray < 25).mean()),
        "bright_ratio": float((gray > 235).mean()),
        "blur_lap_var": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    }


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


def compare(base_frame, current_frame):
    b = feature(base_frame)
    c = feature(current_frame)
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
    if brightness_delta > 55:
        quality_flags.append("large_brightness_shift")

    change_score = (
        min(gray_diff / 45.0, 1.0) * 0.35
        + min(hist_diff / 0.55, 1.0) * 0.25
        + min(block_ratio / 0.45, 1.0) * 0.30
        + min(edge_diff / 0.35, 1.0) * 0.10
    )
    status = "normal"
    event = "none"
    if quality_flags:
        status = "alarm"
        event = quality_flags[0]
    elif change_score >= 0.62 and block_ratio >= 0.28:
        status = "alarm"
        event = "scene_changed"
    elif change_score >= 0.45 and block_ratio >= 0.20:
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
        "changed_blocks": rects[:30],
    }


def draw_overlay(base_frame, current_frame, result, out_path):
    h, w = current_frame.shape[:2]
    canvas = current_frame.copy()
    color = (0, 0, 255) if result["status"] == "alarm" else (0, 200, 255) if result["status"] == "warning" else (0, 180, 0)
    for x1, y1, x2, y2, _score in result["changed_blocks"]:
        sx1, sy1 = int(x1 * w / 320), int(y1 * h / 180)
        sx2, sy2 = int(x2 * w / 320), int(y2 * h / 180)
        cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), color, 2)
    text = f"{result['status']} {result['event']} score={result['change_score']} blocks={result['block_change_ratio']}"
    cv2.rectangle(canvas, (8, 8), (min(w - 8, 8 + len(text) * 12), 44), (0, 0, 0), -1)
    cv2.putText(canvas, text, (14, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)
    cv2.imwrite(str(out_path), canvas)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/tmp/m101_change_test")
    parser.add_argument("--wait", type=float, default=8.0)
    parser.add_argument("--channels-json", required=True)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    channels = json.loads(args.channels_json)
    report = {"channels": {}, "comparisons": {}}

    baselines = {}
    currents = {}
    for ch, meta in channels.items():
        frame = capture_frame(meta["url"])
        baselines[ch] = frame
        cv2.imwrite(str(out_dir / f"ch{ch}_baseline.jpg"), frame)
        report["channels"][ch] = {"location": meta.get("location", ""), "url_tail": meta["url"].split("@")[-1]}

    time.sleep(args.wait)

    for ch, meta in channels.items():
        frame = capture_frame(meta["url"])
        currents[ch] = frame
        cv2.imwrite(str(out_dir / f"ch{ch}_current.jpg"), frame)
        res = compare(baselines[ch], frame)
        report["comparisons"][f"ch{ch}_same_channel"] = res
        draw_overlay(baselines[ch], frame, res, out_dir / f"ch{ch}_same_overlay.jpg")

    ch_keys = list(channels.keys())
    if len(ch_keys) >= 2:
        a, b = ch_keys[0], ch_keys[1]
        res_ab = compare(baselines[a], currents[b])
        report["comparisons"][f"ch{a}_baseline_vs_ch{b}_current"] = res_ab
        draw_overlay(baselines[a], currents[b], res_ab, out_dir / f"ch{a}_vs_ch{b}_overlay.jpg")
        res_ba = compare(baselines[b], currents[a])
        report["comparisons"][f"ch{b}_baseline_vs_ch{a}_current"] = res_ba
        draw_overlay(baselines[b], currents[a], res_ba, out_dir / f"ch{b}_vs_ch{a}_overlay.jpg")

    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
