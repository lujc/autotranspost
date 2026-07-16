#!/usr/bin/env python3
"""pipeline.py — single-entry orchestrator for the autotranspost skill.

Why this exists
---------------
Previously the deterministic post-download stages (hard-subtitle band
detection → subtitle render → burn → publish) were run as separate agent
steps, and the *download* was launched as a background task that the agent
waited on. That introduced a failure mode where, after the download finished,
the pipeline would idle for many minutes until the agent was manually nudged
to continue — because the "completion notification" did not reliably re-trigger
the agent.

This script removes that failure mode by collapsing everything that does NOT
require the LLM into foreground commands the agent runs in one shot:

  pipeline.py download -- <fetch_video.py args...>   # download + cover + subs
  pipeline.py finalize  --job DIR --cn-title T [--publish] [--proxy P]

The only step still driven by the agent is the *translation* (it needs the
LLM): `subtitle_pipeline.py next-batch` → write `translation-output/batch-*.json`
→ repeat. That loop is fast and inline, so it is never backgrounded.

Net effect: the whole flow is two foreground commands plus a translation loop.
There is no long-running background task to "wait on", so the pipeline cannot
park itself. All stages run in this process (via the sibling scripts) and the
command returns only when fully done.

The safe-delete shim is installed at import so temp-file cleanup never aborts
the run, even inside a sandbox that intercepts deletions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import install_safe_delete
install_safe_delete()

PY = sys.executable


def _run_script(name: str, args: list[str], proxy: str | None = None) -> None:
    cmd = [PY, str(SCRIPT_DIR / name), *args]
    env = dict(os.environ)
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    print(f"\n=== {name} {' '.join(args)} ===", flush=True)
    proc = subprocess.run(cmd, env=env, check=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"{name} 退出码 {proc.returncode}")


def _find_master(job: Path) -> Path:
    cands = sorted(job.glob("*.master.mp4"))
    if not cands:
        cands = [p for p in job.glob("*.mp4") if ".master." in p.name]
    if not cands:
        raise SystemExit(f"未在 {job} 找到 master.mp4（请先运行 download）")
    return cands[0]


def _derive_title(master: Path) -> str:
    # "<Title> [id].master.mp4" -> "<Title>"
    stem = master.name
    if stem.endswith(".master.mp4"):
        stem = stem[: -len(".master.mp4")]
    stem = re.sub(r"\s*\[[^\]]+\]$", "", stem)
    return stem


def _resolve_ffmpeg(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("找不到 ffmpeg，请用 --ffmpeg 指定路径或将其加入 PATH")
    return found


def cmd_finalize(args) -> None:
    job = Path(args.job).expanduser().resolve()
    if not job.is_dir():
        raise SystemExit(f"job 目录不存在：{job}")

    master = _find_master(job)
    print(f"master: {master}")
    ffmpeg = _resolve_ffmpeg(args.ffmpeg)
    hardsub = job / "hardsub_band.json"

    # Stage 1: hard-subtitle band detection (Chinese subtitles avoid it).
    _run_script(
        "detect_hardsub_band.py",
        [str(master), "--out", str(hardsub), "--ffmpeg", ffmpeg],
    )

    # Guard: translations must already exist (the agent performs translation
    # via the LLM before invoking finalize). Fail loudly otherwise.
    manifest = job / "subtitles" / "subtitle-manifest.json"
    trans_dir = job / "subtitles" / "translation-output"
    if not manifest.is_file():
        raise SystemExit(f"未找到字幕 manifest：{manifest}（请先运行 download 生成）")
    batches = sorted(trans_dir.glob("batch-*.json")) if trans_dir.is_dir() else []
    if not batches:
        raise SystemExit(
            "未找到翻译结果（subtitles/translation-output/batch-*.json）。\n"
            "请先用 `subtitle_pipeline.py next-batch` 取批次、翻译并写回，再运行 finalize。"
        )

    # Stage 2: render (Chinese-only zh-CN.ass).
    out_dir = job / "subtitles" / "rendered"
    out_dir.mkdir(parents=True, exist_ok=True)
    _run_script(
        "subtitle_pipeline.py",
        [
            "render",
            "--manifest", str(manifest),
            "--translations-dir", str(trans_dir),
            "--output-dir", str(out_dir),
            "--font", args.font,
            "--hardsub", str(hardsub),
        ],
    )
    ass = out_dir / "zh-CN.ass"
    if not ass.is_file():
        raise SystemExit(f"渲染未产出 {ass}")

    # Stage 3: burn (+ optional publish).
    title = _derive_title(master)
    burn = job / f"字幕版「{title}」.mp4"
    burn_args = [str(master), str(ass), str(burn)]
    if args.publish:
        burn_args += ["--publish", "--cn-title", args.cn_title or title]
    _run_script("burn_subtitles.py", burn_args, proxy=args.proxy)

    print(f"\n✅ 字幕版成品：{burn}")
    result = job / "publish-result.json"
    if args.publish and result.is_file():
        try:
            meta = json.loads(result.read_text(encoding="utf-8"))
            print(f"✅ B 站投稿：{meta.get('bvid')} (aid={meta.get('aid')})")
        except Exception:
            pass


def cmd_download(args) -> None:
    rest = list(args.rest)
    if rest and rest[0] == "--":
        rest = rest[1:]
    _run_script("fetch_video.py", rest)


def main(argv) -> None:
    p = argparse.ArgumentParser(prog="pipeline.py", description="autotranspost 流水线编排")
    sub = p.add_subparsers(dest="cmd", required=True)

    fz = sub.add_parser("finalize", help="检测→渲染→烧录→(发布) 合一步骤（前台）")
    fz.add_argument("--job", required=True, help="job 目录")
    fz.add_argument("--cn-title", default="", help="B 站中文标题（--publish 时使用）")
    fz.add_argument("--publish", action="store_true", help="烧录后自动发布 B 站")
    fz.add_argument("--proxy", default=None, help="代理地址，如 http://127.0.0.1:7890")
    fz.add_argument("--font", default="MiSans", help="渲染字体")
    fz.add_argument("--ffmpeg", default=None, help="ffmpeg 路径（默认用 PATH 解析）")
    fz.set_defaults(func=cmd_finalize)

    dl = sub.add_parser("download", help="转发至 fetch_video.py 下载（前台）")
    dl.add_argument("rest", nargs=argparse.REMAINDER, help="传给 fetch_video.py 的参数")
    dl.set_defaults(func=cmd_download)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
