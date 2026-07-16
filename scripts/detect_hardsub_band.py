#!/usr/bin/env python3
"""detect_hardsub_band.py — 判断原视频「底部区域」是否存在硬烧录（画面内嵌）字幕。

设计原则（按用户要求）：
- 字幕通常固定在画面底部区域；只需抽取少量帧、只检查底部区域即可判断。
- 字幕颜色不限：白色 / 黑色 / 彩色都算。因此对 RGB 三个通道分别计算梯度、
  取逐像素最大值，任何颜色的文字笔画都会产生高对比边缘，从而避免漏检。
- 本脚本「只回答一个问题」：底部区域到底有没有长期存在的字幕带？
  - 有 → 输出该带的顶部位置（供渲染脚本把中文字幕抬高到其上方避让）。
  - 没有 → 输出 bottom_has_subtitle=false（中文字幕落到常规底部即可）。
- 不做「顶部/中部避让」，中文字幕一律在底部区域，绝不放到顶部。

为什么用「逐帧细带 + 密度加权投票」（而非过去的全帧平均）：
- 硬烧录字幕往往**间歇出现**（只在讲解/对白时出现），若把所有帧的「底部行
  密度」做平均，字幕信号会被背景纹理的平滑基线稀释，容易判成「无字幕」。
- 底部区域不只是字幕，还有地板、墙体、器械、人物身体等大量高对比结构。
  它们的尺度（高度）远大于字幕细带，峰值密度通常低于清晰的白/黑字幕文字。
- 因此本脚本对每一帧独立找「细带」（高度落在字幕范围、边缘密度够高），
  再按「峰值密度加权」投票统计这些细带在垂直方向上的落点；真正的字幕因为
  固定在同一个位置、且文字笔画的峰值密度最高，会在加权统计里胜出。

这不是 OCR，不识别文字内容，只估计底部条带是否长期出现字幕样的高对比文本。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Install the best-effort delete shim before any work (no-op on normal hosts).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import install_safe_delete
install_safe_delete()

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 numpy：pip install numpy\n")
    raise

FFMPEG = "ffmpeg"  # 由调用方 PATH 提供；也可用 --ffmpeg 指定完整路径

# 只分析画面最底部的这个比例（0.35 = 底部 35%）。
BOTTOM_REGION_FRAC = 0.35
# 边缘阈值：文字笔画 = 高对比小结构。对彩色字幕用逐通道最大梯度后仍适用。
EDGE_THRESHOLD = 38.0
# 一行被认为是「文本样」所需的最低边缘像素占比（越高越严格）。
ROW_EDGE_FRAC = 0.08
# 候选「细带」高度占全帧比例的范围（字幕通常是一两行细带，不是大色块）。
MIN_BAND_H = 0.015
MAX_BAND_H = 0.12
# 候选细带峰值密度下限：清晰的白/黑/黄字幕笔画梯度远高于普通背景边缘。
MIN_PEAK = 0.20
# 候选细带必须落在画面的这个相对高度区间（真字幕贴近底部；地板/墙边多在
# 0.65–0.74，最底部 0.97–1.0 常是画框边，都不算字幕）。
MIN_CENTER_Y = 0.74
MAX_CENTER_Y = 0.97
# 滑动窗口：找字幕锚点时，每个强带周围 ±ANCHOR_WIN 内的强带密度；
# 确定最终字幕带范围时取锚点 ±EXTENT_WIN（覆盖两行字幕，同时排除离群背景）。
ANCHOR_WIN = 0.05
EXTENT_WIN = 0.06
# 稳定性判定：只有当「锚点 ±STD_WIN 内的强带」在逐帧里位置足够稳定
# （标准差 ≤ STABLE_STD）时，才认定为「固定字幕行」并避让；否则视为散落的
# 画面文字/图形，不抬高中文字幕（避免中文被无谓地推高）。
STD_WIN = 0.04
STABLE_STD = 0.02
# 胜出字幕块至少需要支持的帧数，避免单帧误判。
MIN_FRAMES = 3
# 把中文字幕抬高到原字幕带上方的间隙（占全帧高度比例，约 27px@1080p）。
GAP_FRAC = 0.025


def extract_frames(video: Path, out_dir: Path, interval: float, cap: int, ffmpeg: str = "ffmpeg") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(video),
        "-vf", f"fps=1/{interval:.6f},scale=320:-1,format=rgb24",
        "-frames:v", str(cap),
        str(out_dir / "f%05d.png"),
    ]
    subprocess.run(cmd, check=True)
    return sorted(out_dir.glob("f*.png"))


def row_density(img_path: Path) -> np.ndarray | None:
    """返回全帧每行的「文字边缘像素占比」剖面（长度 = 帧高）。

    对 RGB 三通道分别求横/纵梯度后取逐像素最大值，从而对所有颜色的字幕都
    敏感（白字、黑字、黄字、红字……）。
    """
    from PIL import Image

    try:
        im = Image.open(img_path).convert("RGB")
    except Exception:
        return None
    arr = np.asarray(im, dtype=np.float32)  # h, w, 3
    h, w = arr.shape[:2]
    if h < 8 or w < 8:
        return None
    gx = np.abs(np.diff(arr, axis=1))  # h, w-1, 3
    gy = np.abs(np.diff(arr, axis=0))  # h-1, w, 3
    gx = np.pad(gx, ((0, 0), (0, 1), (0, 0)))
    gy = np.pad(gy, ((0, 1), (0, 0), (0, 0)))
    g = np.maximum(gx, gy).max(axis=2)  # h, w 逐像素最大梯度
    edges = g > EDGE_THRESHOLD
    return edges.sum(axis=1).astype(np.float32) / w  # 每行边缘占比


def _thin_bands(reg: np.ndarray, start_row: int, full_h: int) -> list[dict]:
    """对「底部区域」的逐行密度剖面，找出符合字幕特征的细带。

    reg 是从全帧第 start_row 行开始的切片（长度 = full_h - start_row）。
    返回的 {top, bot, center, height} 一律换算到全帧 0–1 坐标 / 占比。
    仅保留高度落在字幕范围、且峰值密度够高的连续段。
    """
    active = reg > ROW_EDGE_FRAC
    if not bool(active.any()):
        return []
    bands: list[tuple[int, int]] = []
    start = None
    for i in range(len(reg)):
        if active[i] and start is None:
            start = i
        elif not active[i] and start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, len(reg)))
    out: list[dict] = []
    for a, b in bands:
        height = (b - a) / full_h
        if not (MIN_BAND_H <= height <= MAX_BAND_H):
            continue
        peak = float(reg[a:b].max())
        out.append({
            "top": (start_row + a) / full_h,
            "bot": (start_row + b) / full_h,
            "center": (start_row + (a + b) / 2) / full_h,
            "peak": peak,
        })
    return out


# 位置聚类间距：落点中心相距 ≤ 此值的强带视为同一字幕块（容纳一行内多笔画/两行字幕）。
CLUSTER_GAP = 0.08


def detect(profiles: list[np.ndarray], min_h: int) -> dict:
    """对多帧的底部细带做「峰值密度加权投票」，返回字幕判定结果。

    思路：
    1. 逐帧在底部区域找「细带」（高度在字幕范围、边缘密度够高），记录每条的
       全帧落点 center、峰值密度 peak、上下边界 top/bot。
    2. 早筛：只保留 center ≥ MIN_CENTER_Y 的细带（真实底部字幕贴近画面最下方，
       中部 0.65–0.74 的地板/墙边粗边直接排除）。
    3. 强带筛选：只保留 peak ≥ MIN_PEAK 的细带。清晰的白/黑/黄字幕笔画梯度
       远高于普通背景边缘，这一步就把绝大多数背景结构剔掉。
    4. 位置就近聚类：把落点相距 ≤ CLUSTER_GAP 的强带聚成「字幕块」（容纳两行字幕）。
    5. 取权重（峰值之和）最高的字幕块作为结果；若支持帧数不足则判无字幕。
    """
    start = int(round((1 - BOTTOM_REGION_FRAC) * min_h))
    collected: list[dict] = []
    for fi, p in enumerate(profiles):
        reg = p[start:min_h].astype(np.float32)
        if reg.shape[0] == 0:
            continue
        for band in _thin_bands(reg, start, min_h):
            if band["center"] >= MIN_CENTER_Y:
                band["frame"] = fi
                collected.append(band)
    if not collected:
        return {
            "bottom_has_subtitle": False,
            "bottom_band": None,
            "reliable": True,
            "method": "bottom-thin-band-density-voting",
            "bands_found": 0,
            "note": "底部区域未检测到任何「字幕样细带」；中文字幕落到常规底部。",
        }

    # 强带筛选：峰值密度够高（字幕文字笔画）才进入投票；并排除最底部画框边。
    strong = [b for b in collected if b["peak"] >= MIN_PEAK and b["center"] <= MAX_CENTER_Y]
    if len(strong) < MIN_FRAMES:
        return {
            "bottom_has_subtitle": False,
            "bottom_band": None,
            "reliable": True,
            "method": "bottom-thin-band-density-voting",
            "bands_found": len(collected),
            "strong_bands": len(strong),
            "note": (
                "底部虽检测到细带，但通过「峰值密度够高(≥%.2f)且持续出现(≥%d帧)」"
                "的强字幕带不足；按「无底部字幕」处理。" % (MIN_PEAK, MIN_FRAMES)
            ),
        }

    # 滑动窗口密度峰：以每个强带为中心，统计 ±ANCHOR_WIN 内的强带数，
    # 取密度最高的位置作为「字幕锚点」（即字幕主行落点，天然压制稀疏的背景簇）。
    anchor_win = ANCHOR_WIN
    best_anchor, best_count = 0.0, -1
    for b in strong:
        c = b["center"]
        cnt = sum(1 for x in strong if abs(x["center"] - c) <= anchor_win)
        if cnt > best_count:
            best_count, best_anchor = cnt, c
    if best_count < MIN_FRAMES:
        return {
            "bottom_has_subtitle": False,
            "bottom_band": None,
            "reliable": True,
            "method": "bottom-thin-band-density-voting",
            "bands_found": len(collected),
            "strong_bands": len(strong),
            "note": (
                "字幕锚点周围(±%.2f)的强带数(%d)不足(≥%d)；按「无底部字幕」处理。"
                % (anchor_win, best_count, MIN_FRAMES)
            ),
        }

    # 稳定性判定：取锚点 ±STD_WIN 内的强带，按帧取各自的代表中心，
    # 若逐帧位置标准差过大，说明是散落画面文字而非固定字幕行，不当字幕避让。
    tight = [b for b in strong if abs(b["center"] - best_anchor) <= STD_WIN]
    if len(tight) < MIN_FRAMES:
        return {
            "bottom_has_subtitle": False,
            "bottom_band": None,
            "reliable": True,
            "method": "bottom-thin-band-density-voting",
            "bands_found": len(collected),
            "strong_bands": len(strong),
            "note": (
                "锚点附近(±%.2f)强带不足(=%d, ≥%d)或位置不稳定；"
                "按「无底部字幕」处理。" % (STD_WIN, len(tight), MIN_FRAMES)
            ),
        }
    by_frame: dict[int, list[float]] = {}
    for b in tight:
        by_frame.setdefault(b.get("frame", -1), []).append(b["center"])
    frame_centers = [float(np.mean(cs)) for cs in by_frame.values()]
    std = float(np.std(frame_centers)) if len(frame_centers) > 1 else 0.0
    if std > STABLE_STD:
        return {
            "bottom_has_subtitle": False,
            "bottom_band": None,
            "reliable": True,
            "method": "bottom-thin-band-density-voting",
            "bands_found": len(collected),
            "strong_bands": len(strong),
            "note": (
                "锚点附近强带位置不稳定(std=%.4f > %.4f)，判定为散落画面文字而非固定字幕行；"
                "按「无底部字幕」处理。" % (std, STABLE_STD)
            ),
        }

    # 以锚点 ±EXTENT_WIN 取最终字幕带范围（覆盖两行字幕，同时排除离群背景）。
    use = [b for b in strong if abs(b["center"] - best_anchor) <= EXTENT_WIN]
    if len(use) < MIN_FRAMES:
        use = tight  # 兜底：极少发生

    if os.environ.get("DETECT_DEBUG"):
        sys.stderr.write(
            f"[detect] strong_centers={np.round([b['center'] for b in strong], 3).tolist()}\n"
            f"[detect] anchor={best_anchor:.3f} count={best_count} tight_n={len(tight)} std={std:.4f} use_n={len(use)}\n"
        )

    y0 = min(b["top"] for b in use)
    y1 = max(b["bot"] for b in use)
    center = float(np.mean([b["center"] for b in use]))
    height = y1 - y0
    peak_mean = float(np.mean([b["peak"] for b in use]))
    bottom_band = {
        "y0": round(float(y0), 4),
        "y1": round(float(y1), 4),
        "center": round(center, 4),
        "height": round(float(height), 4),
        "peak_density": round(peak_mean, 4),
    }
    return {
        "bottom_has_subtitle": True,
        "bottom_band": bottom_band,
        "reliable": True,
        "method": "bottom-thin-band-density-voting",
        "bands_found": len(collected),
        "strong_bands": len(strong),
        "support_frames": len(use),
        "note": (
            f"底部区域检测到稳定字幕带（y0={bottom_band['y0']}, "
            f"y1={bottom_band['y1']}，峰值密度={bottom_band['peak_density']}，"
            f"支持帧数={len(use)}）；中文字幕将抬高到该带上方避让。"
        ),
    }


def _write_result(out: Path, result: dict, video: Path, frames_analyzed: int) -> None:
    result_full = {
        "video": str(video),
        "analyzed_region": "bottom",
        "region_y0": round(1 - BOTTOM_REGION_FRAC, 4),
        "region_y1": 1.0,
        "frames_analyzed": frames_analyzed,
        "recommend": (
            {"placement": "bottom_above", "alignment": 2, "margin_v": None}
            if result.get("bottom_has_subtitle")
            else {"placement": "bottom", "alignment": 2, "margin_v": None}
        ),
        **result,
    }
    out.write_text(json.dumps(result_full, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"硬字幕检测：bottom_has_subtitle={result_full['bottom_has_subtitle']} "
        f"(reliable={result_full['reliable']}, bands_found={result_full.get('bands_found')}) -> {out}"
    )


def _no_sub_result(out: Path, video: Path, note: str, frames_analyzed: int = 0) -> None:
    result = {
        "bottom_has_subtitle": False,
        "bottom_band": None,
        "reliable": False,
        "method": "bottom-thin-band-density-voting",
        "bands_found": 0,
        "note": note,
    }
    _write_result(out, result, video, frames_analyzed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="判断原视频底部区域是否含硬烧录字幕")
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
                _no_sub_result(out, video, "未能抽出任何帧，按无底部字幕处理。")
                return 0
            profiles: list[np.ndarray] = []
            for f in frames:
                d = row_density(f)
                if d is not None:
                    profiles.append(d)
            if not profiles:
                raise RuntimeError("所有帧均无法解析")
            min_h = min(p.shape[0] for p in profiles)
            result = detect(profiles, min_h)
    except Exception as e:  # pragma: no cover
        _no_sub_result(out, video, f"检测异常（{e}），已按无底部字幕处理。")
        return 0

    _write_result(out, result, video, len(profiles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
