#!/usr/bin/env python3
"""detect_hardsub_band.py — 估计原视频中「硬烧录（画面内嵌）字幕」所占的竖向区域。

方法：用 ffmpeg 按固定间隔抽帧（灰度、缩小到 320px 宽），对每帧计算
「每行文本/边缘密度」的竖向剖面；跨所有帧聚合后，找出持续出现的高密度带，
即硬字幕最可能所在的 band。输出 JSON，供 subtitle_pipeline 把翻译字幕放到「对侧」。

这不是 OCR，不识别具体文字，只估计「画面长期在哪一竖向条带出现字幕样的高对比文本」。
对绝大多数「底部字幕」的视频有效；检测失败或不可靠时返回 reliable=false，
调用方应回退到画面上方（安全默认）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 numpy：pip install numpy\n")
    raise

FFMPEG = "ffmpeg"  # 由调用方 PATH 提供；也可用 --ffmpeg 指定完整路径


def extract_frames(video: Path, out_dir: Path, interval: float, cap: int, ffmpeg: str = "ffmpeg") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(video),
        "-vf", f"fps=1/{interval:.6f},scale=320:-1,format=gray",
        "-frames:v", str(cap),
        str(out_dir / "f%05d.png"),
    ]
    subprocess.run(cmd, check=True)
    return sorted(out_dir.glob("f*.png"))


def row_density(img_path: Path) -> np.ndarray | None:
    from PIL import Image

    try:
        im = Image.open(img_path).convert("L")
    except Exception:
        return None
    arr = np.asarray(im, dtype=np.float32)
    h, w = arr.shape
    if h < 8 or w < 8:
        return None
    # 横向与纵向梯度 -> 边缘强度（文字笔画 = 高对比小结构）
    gx = np.abs(np.diff(arr, axis=1))
    gy = np.abs(np.diff(arr, axis=0))
    gx = np.pad(gx, ((0, 0), (0, 1)))
    gy = np.pad(gy, ((0, 1), (0, 0)))
    edges = (gx + gy) > 38.0
    # 每行文本像素占比
    return edges.sum(axis=1).astype(np.float32) / w


def find_band(agg: np.ndarray) -> dict:
    h = agg.shape[0]
    mean = agg.mean()
    std = agg.std()
    thr = max(mean + 1.5 * std, 0.30 * agg.max(), 1e-4)
    active = agg > thr
    if not bool(active.any()):
        return {"found": False}
    # 连续的活跃行段
    bands: list[tuple[int, int]] = []
    start = None
    for i in range(h):
        if active[i] and start is None:
            start = i
        elif not active[i] and start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, h))
    if not bands:
        return {"found": False}
    # 选「跨帧最持续」的段：峰值密度 * 段宽 作为评分
    best = None
    best_score = -1.0
    for (a, b) in bands:
        seg = agg[a:b]
        score = float(seg.max()) * (b - a)
        if score > best_score:
            best_score = score
            best = (a, b)
    a, b = best
    center = (a + b) / 2.0 / h
    return {
        "found": True,
        "y0": round(a / h, 4),
        "y1": round(b / h, 4),
        "center": round(center, 4),
        "height": round((b - a) / h, 4),
        "peak_density": round(float(agg[a:b].max()), 4),
    }


def recommend(band: dict) -> dict:
    if not band.get("found"):
        return {
            "side": "none", "recommend_alignment": 8, "margin_v": 60,
            "reliable": False,
            "note": "未检测到稳定字幕带，回退到画面上方（安全默认）。",
        }
    c = band["center"]
    if c > 0.6:
        side, align, mv = "bottom", 8, 60   # 原字幕在底部 -> 中文放顶部
    elif c < 0.4:
        side, align, mv = "top", 2, 50      # 原字幕在顶部 -> 中文放底部
    else:
        side, align, mv = "middle", 8, 60   # 中部 -> 顶部（安全默认）
    return {"side": side, "recommend_alignment": align, "margin_v": mv, "reliable": True}


def _write_fallback(out: Path, note: str) -> None:
    rec = recommend({"found": False})
    out.write_text(
        json.dumps(
            {
                "video": None, "band": None,
                "recommend_alignment": rec["recommend_alignment"],
                "margin_v": rec["margin_v"], "side": "none",
                "reliable": False, "method": "text-density-band", "note": note,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="估计原视频硬烧录字幕区域（竖向带）")
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="输出 hardsub_band.json 路径")
    ap.add_argument("--interval", type=float, default=10.0, help="抽帧间隔（秒）")
    ap.add_argument("--cap", type=int, default=120, help="最多抽帧数")
    ap.add_argument("--ffmpeg", default=FFMPEG)
    args = ap.parse_args(argv)

    video = args.video.expanduser().resolve()
    if not video.exists():
        sys.stderr.write(f"视频不存在：{video}\n")
        return 1
    out = args.out or (video.parent / "hardsub_band.json")

    try:
        with tempfile.TemporaryDirectory() as td:
            frames = extract_frames(video, Path(td), args.interval, args.cap, args.ffmpeg)
            if not frames:
                sys.stderr.write("未能抽出任何帧，回退到顶部放置。\n")
                _write_fallback(out, "未能抽出任何帧，已回退顶部。")
                return 0
            profiles: list[np.ndarray] = []
            for f in frames:
                d = row_density(f)
                if d is not None:
                    profiles.append(d)
            if not profiles:
                raise RuntimeError("所有帧均无法解析")
            min_h = min(p.shape[0] for p in profiles)
            agg = np.zeros(min_h, dtype=np.float32)
            for p in profiles:
                agg += p[:min_h]
            agg /= len(profiles)
            band = find_band(agg)
    except Exception as e:  # pragma: no cover
        sys.stderr.write(f"硬字幕检测失败（{e}），回退到顶部放置。\n")
        _write_fallback(out, f"检测异常（{e}），已回退顶部。")
        return 0

    rec = recommend(band)
    result = {
        "video": str(video),
        "band": band if band.get("found") else None,
        "recommend_alignment": rec["recommend_alignment"],
        "margin_v": rec["margin_v"],
        "side": rec["side"],
        "reliable": rec["reliable"],
        "method": "text-density-band",
        "frames_analyzed": len(profiles),
        "note": rec.get("note", ""),
    }
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"硬字幕检测：side={rec['side']} recommend_alignment="
        f"{rec['recommend_alignment']} (reliable={rec['reliable']}) -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
