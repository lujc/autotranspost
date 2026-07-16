#!/usr/bin/env python3
"""publish_bilibili.py — upload + submit a finished (subtitled) MP4 to Bilibili.

Part of the autotranspost skill. By default this runs automatically after a
successful burn (see burn_subtitles.py --publish); it can also be invoked
manually to re-upload or publish a specific MP4.

Subcommands:
  login [--generate] [--confirm] [--timeout S]
        QR-code login. `--generate` writes cache/bilibili_qr.png and prints
        the path + the raw URL (agent shows the image for the user to scan).
        `--confirm` polls the saved qrcode_key until success and then caches
        SESSDATA / bili_jct / buvid3 to cache/bilibili_credential.json.
        Default = generate then confirm in one run.
  status
        Show whether the cached credential is still logged in.
  publish --job DIR [--video F] [--cover F] [--meta F] [--dry-run]
        Upload + submit the burned MP4 inside DIR.

Conventions (also documented in SKILL.md):
  * Publishing is OPT-IN only — never call this without an explicit user request.
  * Translated / re-published videos default to copyright=2 (转载) WITH a
    source URL. Use copyright=1 (原创) ONLY when you own the rights.
  * The credential cache stores only SESSDATA / bili_jct / buvid3, is
    git-ignored (cache/), and is never printed in clear text (Invariant #5
    of SKILL.md extends to it).

Implementation notes:
  * Uses the `bilibili_api` package (low-level functions video.video_upload /
    video.video_cover_upload / video.video_submit + utils.Verify).
  * QR login is implemented directly against Bilibili's passport QR API because
    the library has no built-in login helper.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.parse as up
from pathlib import Path

import requests
from bilibili_api import video, utils
from bilibili_api.utils import Verify

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except Exception:  # pragma: no cover
    _HAS_PIL = False

SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = SKILL_ROOT / "cache"
CRED_PATH = CACHE_DIR / "bilibili_credential.json"
QR_KEY_PATH = CACHE_DIR / "bilibili_qr_key.json"
QR_PNG_PATH = CACHE_DIR / "bilibili_qr.png"

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
QR_GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


# --------------------------------------------------------------------------
# Credential cache (secret — never printed in clear)
# --------------------------------------------------------------------------
def _ensure_cache() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_credential() -> dict | None:
    if not CRED_PATH.exists():
        return None
    try:
        return json.loads(CRED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_credential(d: dict) -> None:
    _ensure_cache()
    CRED_PATH.write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        CRED_PATH.chmod(0o600)
    except Exception:
        pass


def make_verify(cred: dict) -> Verify:
    return Verify(
        sessdata=cred.get("sessdata"),
        csrf=cred.get("csrf") or cred.get("bili_jct"),
        buvid3=cred.get("buvid3"),
    )


def check_login(verify: Verify) -> tuple[bool, str]:
    """Return (is_logged_in, uname_or_error)."""
    try:
        r = requests.get(
            NAV_URL, headers=HEADERS, cookies=verify.get_cookies(), timeout=10
        )
        j = r.json()
        if j.get("code") == 0 and j.get("data", {}).get("isLogin"):
            return True, j["data"].get("uname", "")
    except Exception as e:
        return False, f"nav error: {e}"
    return False, "not logged in"


def _mask(s: str | None) -> str:
    if not s:
        return "<empty>"
    return s[:4] + "…" + s[-4:] if len(s) > 8 else "***"


# --------------------------------------------------------------------------
# QR login (implemented against passport API; lib has no built-in login)
# --------------------------------------------------------------------------
def _save_qr_key(key: str) -> None:
    _ensure_cache()
    QR_KEY_PATH.write_text(
        json.dumps({"qrcode_key": key}, ensure_ascii=False), encoding="utf-8"
    )


def _load_qr_key() -> str | None:
    if not QR_KEY_PATH.exists():
        return None
    try:
        return json.loads(QR_KEY_PATH.read_text(encoding="utf-8")).get("qrcode_key")
    except Exception:
        return None


def _qr_generate(session: requests.Session) -> str:
    r = session.get(QR_GEN_URL, headers=HEADERS, timeout=10)
    j = r.json()
    if j.get("code") != 0:
        sys.stderr.write(f"QR generate failed: {j}\n")
        sys.exit(1)
    url = j["data"]["url"]
    key = j["data"]["qrcode_key"]
    _save_qr_key(key)
    # Render a scannable QR image so the agent can show it to the user.
    import qrcode

    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    _ensure_cache()
    img.save(str(QR_PNG_PATH))
    print("请使用 B 站 App 扫描下方二维码登录：")
    print(f"  二维码图片：{QR_PNG_PATH}")
    print(f"  二维码内容（备用）：{url}")
    return key


def _extract_cookies(session: requests.Session, j: dict) -> dict:
    cj = session.cookies
    cred = {
        "sessdata": cj.get("SESSDATA"),
        "csrf": cj.get("bili_jct"),
        "buvid3": cj.get("buvid3"),
    }
    # Fallback: some flows put the token in the success redirect URL query string.
    if not cred["sessdata"]:
        u = (j.get("data") or {}).get("url", "")
        q = up.urlparse(u).query
        d = up.parse_qs(q)
        cred["sessdata"] = (d.get("SESSDATA") or [None])[0]
        cred["csrf"] = cred["csrf"] or (d.get("bili_jct") or [None])[0]
    return cred


def _qr_poll(session: requests.Session, qrcode_key: str, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = session.get(
                QR_POLL_URL,
                params={"qrcode_key": qrcode_key},
                headers=HEADERS,
                timeout=10,
            )
            j = r.json()
        except Exception as e:
            time.sleep(3)
            continue
        # NOTE: the poll endpoint always returns outer code==0; the real
        # login status lives in data.code (0=success, 86038=expired,
        # 86090=scanned-awaiting-confirm, 86101=not-scanned).
        data = j.get("data") or {}
        code = data.get("code", j.get("code"))
        if code == 0:
            cred = _extract_cookies(session, j)
            if cred.get("sessdata"):
                save_credential(cred)
                ok, uname = check_login(make_verify(cred))
                print(
                    f"登录成功：用户 {_mask(uname)} "
                    f"(SESSDATA {_mask(cred['sessdata'])})"
                )
                if not ok:
                    sys.stderr.write("警告：登录校验返回未登录，请重新 login。\n")
                return
            sys.stderr.write("登录成功但未能提取到 SESSDATA，请重试。\n")
            sys.exit(1)
        elif code == 86038:
            sys.stderr.write("二维码已过期，请重新运行 login --generate。\n")
            sys.exit(1)
        elif code in (86090, 86101, 86102, -1):
            # 86101 未扫描 / 86090 已扫描待确认 / 86102 扫描中 / -1 等待
            time.sleep(2)
            continue
        else:
            sys.stderr.write(f"未知轮询状态 code={code} msg={data.get('message')}\n")
            time.sleep(2)
            continue
    sys.stderr.write("登录超时，请重试。\n")
    sys.exit(1)


def cmd_login(args) -> None:
    _ensure_cache()
    gen = args.generate
    conf = args.confirm
    if not gen and not conf:
        gen = conf = True
    session = requests.Session()
    session.headers.update(HEADERS)
    key = None
    if gen:
        key = _qr_generate(session)
    else:
        key = _load_qr_key()
        if not key:
            sys.stderr.write("未找到保存的 qrcode_key，请先运行 login --generate。\n")
            sys.exit(1)
    if conf:
        _qr_poll(session, key, args.timeout)


def cmd_status(_args) -> None:
    cred = load_credential()
    if not cred or not cred.get("sessdata"):
        print("未登录：请先运行 publish_bilibili.py login")
        return
    ok, uname = check_login(make_verify(cred))
    print(f"登录态：{'有效' if ok else '失效'}  用户：{_mask(uname)}")


# --------------------------------------------------------------------------
# Publish (upload + submit)
# --------------------------------------------------------------------------
def _auto_video(job: Path) -> Path | None:
    for pat in ("字幕版*.mp4", "双语字幕版*.mp4", "*.mp4", "*.mkv"):
        hits = sorted(job.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return hits[0]
    return None


def _manifest_source(job: Path) -> tuple[str, str]:
    """Return (title, source_url) from download-manifest.json, or ('', '')."""
    mpath = job / "download-manifest.json"
    if not mpath.exists():
        return "", ""
    try:
        m = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    src = m.get("source") or {}
    return str(src.get("title", "")), str(src.get("url", ""))


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _wrap_cjk(text: str, max_chars: int) -> list[str]:
    """Greedy line-wrap a CJK title by character count."""
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return lines


def _make_cn_cover(
    base: Path, out: Path, cn_title: str, prefix: str = "转载翻译"
) -> Path | None:
    """Render a Bilibili cover (1146x717) from `base` with a bold Chinese
    title + 'prefix' badge overlay. Returns the output path, or None if PIL
    is unavailable / base missing."""
    if not _HAS_PIL or not base or not base.exists():
        return None
    try:
        img = Image.open(base).convert("RGB")
    except Exception:
        return None
    W, H = 1146, 717
    img = img.resize((W, H))
    draw = ImageDraw.Draw(img, "RGBA")
    # dark gradient scrim bottom->top for legibility
    for y in range(H):
        a = int(150 * (y / H) ** 1.6)
        draw.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    # top light scrim so the badge is readable
    for y in range(0, 120):
        draw.line([(0, y), (W, y)], fill=(0, 0, 0, int(90 * (1 - y / 120))))
    font_dir = "C:/Windows/Fonts"
    title_font = (
        ImageFont.truetype(f"{font_dir}/simhei.ttf", 78)
        if Path(f"{font_dir}/simhei.ttf").exists()
        else ImageFont.load_default()
    )
    badge_font = (
        ImageFont.truetype(f"{font_dir}/msyh.ttc", 40)
        if Path(f"{font_dir}/msyh.ttc").exists()
        else ImageFont.load_default()
    )
    # badge "转载翻译"
    bw, bh = draw.textbbox((0, 0), prefix, font=badge_font)[2:4]
    pad = 18
    bx, by = 60, 60
    draw.rounded_rectangle(
        [bx, by, bx + bw + pad * 2, by + bh + pad * 2],
        radius=14,
        fill=(214, 48, 57, 235),
    )
    draw.text((bx + pad, by + pad), prefix, font=badge_font, fill=(255, 255, 255, 255))
    # title (wrapped, max 11 chars/line, up to 3 lines)
    lines = _wrap_cjk(cn_title, 11)[:3]
    lh = 92
    ty = H - 70 - lh * len(lines)
    for i, line in enumerate(lines):
            draw.text(
                (60, ty + i * lh),
                line,
                font=title_font,
                fill=(255, 255, 0, 255),
                stroke_width=3,
                stroke_fill=(0, 0, 0, 220),
            )
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out))
    return out


def _load_meta(meta_path: Path, video_path: Path, job: Path, cn_title: str = "") -> dict:
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"读取 meta 失败：{e}\n")
            sys.exit(1)
    else:
        # Build a default meta next to the video, for the user to edit.
        # Pull title + source URL from the job manifest when present so a
        # copyright=2 (转载) submission has the required source link.
        m_title, m_source = _manifest_source(job)
        meta = {
            "title": m_title or video_path.stem,
            "desc": "",
            "dynamic": "",
            "tag": "翻译,字幕",
            "tid": 201,
            "copyright": 2,
            "source": m_source,
            "no_reprint": 0,
            "cover": "",
            "subtitles": {"lan": "", "open": 0},
            "part_title": m_title or video_path.stem,
            "part_desc": "",
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"已生成默认 publish-meta.json：{meta_path}\n"
            f"  请按需修改（尤其 copyright=2 时必须填 source 原视频来源链接）。"
        )
    # Apply an explicit Chinese title (overrides the manifest's English one).
    if cn_title:
        cn_title = cn_title.strip()
        meta["title"] = cn_title
        meta["part_title"] = cn_title
        if meta_path.exists():
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"已用中文标题覆盖 meta.title / part_title：{cn_title}")
    return meta


def _validate_meta(meta: dict, meta_path: Path) -> None:
    title = str(meta.get("title", "")).strip()
    if not title:
        sys.stderr.write("meta.title 不能为空。\n")
        sys.exit(1)
    if len(title) > 80:
        meta["title"] = title[:80]
        sys.stderr.write("警告：title > 80 字，已截断。\n")

    tags = [t.strip() for t in str(meta.get("tag", "")).split(",") if t.strip()]
    if len(tags) > 10:
        tags = tags[:10]
        meta["tag"] = ",".join(tags)
        sys.stderr.write("警告：tag > 10 个，已截断为前 10 个。\n")
    else:
        meta["tag"] = ",".join(tags)

    copyright = int(meta.get("copyright", 2))
    if copyright not in (1, 2):
        sys.stderr.write("meta.copyright 必须为 1（原创）或 2（转载）。\n")
        sys.exit(1)
    meta["copyright"] = copyright
    if copyright == 2 and not str(meta.get("source", "")).strip():
        sys.stderr.write(
            "版权=转载(2) 必须填 source（原视频来源链接），否则 B 站会拒绝投稿。\n"
        )
        sys.exit(1)
    try:
        meta["tid"] = int(meta.get("tid", 201))
    except (TypeError, ValueError):
        sys.stderr.write("meta.tid 必须为整数分区 ID。\n")
        sys.exit(1)


def _short_hash(path: Path) -> str:
    h = hashlib.blake2b(digest_size=8)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dry_run(video_path: Path, cover_path: Path | None, meta: dict) -> None:
    size = video_path.stat().st_size
    print("=== DRY-RUN（未实际上传/投稿）===")
    print(f"视频：{video_path}")
    print(f"  大小：{size / 1024 / 1024:.1f} MB")
    print(f"  hash(blake2b-64)：{_short_hash(video_path)}")
    print(f"封面：{cover_path if (cover_path and cover_path.exists()) else '（无/使用默认）'}")
    print(f"标题：{meta['title']}")
    print(f"标签：{meta['tag']}")
    print(f"分区 tid：{meta['tid']}")
    print(f"版权：{'原创(1)' if meta['copyright'] == 1 else '转载(2)'}")
    print(f"来源：{meta.get('source', '') or '（空）'}")
    cred = load_credential()
    if cred and cred.get("sessdata"):
        ok, uname = check_login(make_verify(cred))
        print(f"登录态：{'有效' if ok else '失效'} 用户：{_mask(uname)}")
    else:
        print("登录态：未登录（请先运行 login）")
    print("=== DRY-RUN 结束 ===")


def _on_progress(state: dict, d: dict) -> None:
    ev = d.get("event")
    ok = d.get("ok")
    if ev == "UPLOAD_CHUNK" and ok and isinstance(d.get("data"), dict):
        ch = d["data"]
        n, tot = ch.get("partNumber"), ch.get("chunks")
        if n and tot:
            pct = int(n / tot * 100)
            if pct // 5 > state.get("last", -1) // 5:
                state["last"] = pct
                print(f"  上传 {pct}%", file=sys.stderr)
    elif ev in ("PRE_UPLOAD", "GET_UPLOAD_ID", "VERIFY"):
        print(f"  {ev} ok={ok}", file=sys.stderr)


def _upload_and_submit(
    video_path: Path,
    cover_path: Path | None,
    meta: dict,
    verify: Verify,
    cn_cover_path: Path | None = None,
) -> None:
    # 1) cover — prefer the rendered Chinese-title cover when present.
    effective_cover = cn_cover_path or cover_path
    cover_url = ""
    if effective_cover and effective_cover.exists():
        label = "上传中文封面" if cn_cover_path else "上传封面"
        print(label + "…")
        try:
            cover_url = video.video_cover_upload(str(effective_cover), verify)
            print(f"  封面 URL：{cover_url}")
        except Exception as e:
            sys.stderr.write(f"  {label}失败（将不设置封面）：{e}\n")
    # 2) video (retry up to 3x)
    print("上传视频…")
    filename = None
    state: dict = {}
    for attempt in range(1, 4):
        try:
            filename = video.video_upload(
                str(video_path), verify, on_progress=lambda d: _on_progress(state, d)
            )
            break
        except Exception as e:
            sys.stderr.write(f"  视频上传失败（第 {attempt} 次）：{e}\n")
            if attempt == 3:
                sys.exit(1)
            time.sleep(3 * attempt)
    # 3) submit (retry up to 3x)
    data = {
        "copyright": int(meta["copyright"]),
        "source": str(meta.get("source", "")),
        "cover": cover_url,
        "desc": str(meta.get("desc", "")),
        "desc_format_id": 0,
        "dynamic": str(meta.get("dynamic", "")),
        "interactive": 0,
        "no_reprint": int(meta.get("no_reprint", 0)),
        "subtitles": meta.get("subtitles", {"lan": "", "open": 0}),
        "tag": meta["tag"],
        "tid": int(meta["tid"]),
        "title": meta["title"],
        "videos": [
            {
                "desc": str(meta.get("part_desc", "")),
                "filename": filename,
                "title": str(meta.get("part_title", meta["title"])),
            }
        ],
    }
    last = None
    for attempt in range(1, 4):
        try:
            resp = video.video_submit(data, verify)
            last = resp
            # Success: bilibili returns {"aid":..., "bvid":...} (no code field).
            # The bvid value may already include the "BV" prefix — normalise.
            if isinstance(resp, dict) and resp.get("bvid"):
                bv = resp["bvid"]
                if not bv.startswith("BV"):
                    bv = f"BV{bv}"
                print(f"投稿成功：{bv} (aid={resp.get('aid', '?')})")
                return
            sys.stderr.write(f"  投稿返回异常（第 {attempt} 次）：{resp}\n")
        except Exception as e:
            sys.stderr.write(f"  投稿失败（第 {attempt} 次）：{e}\n")
        if attempt < 3:
            time.sleep(3 * attempt)
    # If the last response still carries a bvid, treat it as success (some
    # error shapes wrap a valid bvid, e.g. rate-limit on a retry).
    if isinstance(last, dict) and last.get("bvid"):
        bv = last["bvid"]
        if not bv.startswith("BV"):
            bv = f"BV{bv}"
        print(f"投稿成功（重试末次）：{bv} (aid={last.get('aid', '?')})")
        return
    sys.stderr.write(f"投稿最终失败，最后响应：{last}\n")
    sys.exit(1)


def cmd_publish(args) -> None:
    job = Path(args.job)
    if not job.is_dir():
        sys.stderr.write(f"job dir 不存在：{job}\n")
        sys.exit(1)
    video_path = Path(args.video) if args.video else _auto_video(job)
    if not video_path or not video_path.exists():
        sys.stderr.write("未找到视频文件，请用 --video 指定烧录好的 MP4。\n")
        sys.exit(1)
    cover_path = Path(args.cover) if args.cover else (job / "封面.jpg")
    meta_path = Path(args.meta) if args.meta else (job / "publish-meta.json")
    meta = _load_meta(meta_path, video_path, job, cn_title=args.cn_title or "")
    _validate_meta(meta, meta_path)

    # Render a Chinese-title cover when a CN title is supplied.
    cn_cover = None
    if args.cn_title and _HAS_PIL:
        base = cover_path if (cover_path and cover_path.exists()) else None
        if base:
            cn_cover = job / "封面_中文.png"
            made = _make_cn_cover(base, cn_cover, args.cn_title.strip())
            if made:
                print(f"已生成中文封面：{made}（将在上传时优先使用）")
            else:
                sys.stderr.write("中文封面生成失败，将回退使用原始封面。\n")
                cn_cover = None
        else:
            sys.stderr.write("未找到原始封面，无法生成中文封面。\n")

    # Dry-run is a pure offline preview; it never requires a live login.
    if args.dry_run:
        _dry_run(video_path, cn_cover or cover_path, meta)
        return

    cred = load_credential()
    if not cred or not cred.get("sessdata"):
        sys.stderr.write("未登录：请先运行 publish_bilibili.py login\n")
        sys.exit(1)
    verify = make_verify(cred)
    ok, uname = check_login(verify)
    if not ok:
        sys.stderr.write(f"登录态失效（{uname}），请重新 login。\n")
        sys.exit(1)
    _upload_and_submit(video_path, cover_path, meta, verify, cn_cover_path=cn_cover)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv) -> None:
    p = argparse.ArgumentParser(
        prog="publish_bilibili.py",
        description="Upload + submit a finished subtitled MP4 to Bilibili.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="QR-code login")
    pl.add_argument("--generate", action="store_true", help="only generate + save QR")
    pl.add_argument("--confirm", action="store_true", help="only poll saved QR key")
    pl.add_argument("--timeout", type=int, default=180, help="poll timeout seconds")

    sub.add_parser("status", help="show cached login state")

    pp = sub.add_parser("publish", help="upload + submit a job's MP4")
    pp.add_argument("--job", required=True, help="job directory")
    pp.add_argument("--video", help="override video file path")
    pp.add_argument("--cover", help="override cover image path")
    pp.add_argument("--meta", help="override publish-meta.json path")
    pp.add_argument(
        "--cn-title",
        help="中文标题；提供后投稿标题与封面上会显示该中文（封面叠加'转载翻译'大字）",
    )
    pp.add_argument("--dry-run", action="store_true", help="validate + preview, no upload")

    args = p.parse_args(argv)
    {"login": cmd_login, "status": cmd_status, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    main(sys.argv[1:])
