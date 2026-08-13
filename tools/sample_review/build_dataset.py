#!/usr/bin/env python3
"""Build a compact review dataset from the local human-review package."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from PIL import Image


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def stable_id(group: str, index: int, filename: str) -> str:
    value = f"{group}\0{index}\0{filename}".encode("utf-8")
    return hashlib.sha1(value).hexdigest()[:20]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_review_image(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        image.save(destination, "JPEG", quality=86, optimize=True)


def build_v17(root: Path, output: Path) -> list[dict[str, object]]:
    group_dir = root / "01_V17训练正样本审计_213"
    records = json.loads((group_dir / "manifest.json").read_text(encoding="utf-8"))
    pages: dict[int, Image.Image] = {}
    items = []
    try:
        for record in records:
            index = int(record["index"])
            page_number = (index - 1) // 25 + 1
            local_index = (index - 1) % 25
            if page_number not in pages:
                page_path = group_dir / "contact-sheets" / f"v17-positive-color-audit-{page_number:02d}.jpg"
                pages[page_number] = Image.open(page_path).convert("RGB")
            page = pages[page_number]
            cell_width = page.width // 5
            cell_height = page.height // 5
            column = local_index % 5
            row = local_index // 5
            crop = page.crop(
                (
                    column * cell_width,
                    row * cell_height,
                    (column + 1) * cell_width,
                    (row + 1) * cell_height,
                )
            )
            item_id = stable_id(group_dir.name, index, Path(record["source_image"]).name)
            image_name = f"{item_id}.jpg"
            crop.save(output / "images" / image_name, "JPEG", quality=90, optimize=True)
            items.append(
                {
                    "id": item_id,
                    "group": group_dir.name,
                    "index": index,
                    "filename": Path(record["source_image"]).name,
                    "image": image_name,
                    "source_image": record["source_image"],
                    "split": record["split"],
                    "sha256": record["sha256"],
                    "decision": "pending",
                }
            )
    finally:
        for page in pages.values():
            page.close()
    return items


def build_other_groups(root: Path, output: Path) -> list[dict[str, object]]:
    items = []
    for group_dir in sorted(root.iterdir()):
        if not group_dir.is_dir() or not group_dir.name[:2].isdigit() or group_dir.name.startswith("01_"):
            continue
        images = sorted(
            path
            for path in group_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
        for index, source in enumerate(images, 1):
            item_id = stable_id(group_dir.name, index, source.name)
            image_name = f"{item_id}.jpg"
            save_review_image(source, output / "images" / image_name)
            items.append(
                {
                    "id": item_id,
                    "group": group_dir.name,
                    "index": index,
                    "filename": source.name,
                    "image": image_name,
                    "source_image": "",
                    "split": "",
                    "sha256": sha256_file(source),
                    "decision": "negative" if group_dir.name.startswith("08_") else "pending",
                }
            )
    return items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    (output / "images").mkdir(parents=True)
    items = build_v17(source, output) + build_other_groups(source, output)
    (output / "manifest.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"items": len(items), "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
