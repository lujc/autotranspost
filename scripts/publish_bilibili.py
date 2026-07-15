#!/usr/bin/env python3
"""publish_bilibili.py — upload + submit a finished bilingual MP4 to Bilibili.

This is an OPT-IN feature of the autopublish skill. It is NEVER called automatically;
the user must explicitly ask to publish.

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
        code = j.get("code")
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
        elif code in (86090, 86101, 86102, -1, 0):
            # 86101 未扫描 / 86090 已扫描待确认 / 86102 扫描中 / -1 等待
            time.sleep(2)
            continue
        else:
            sys.stderr.write(f"未知轮询状态 code={code} msg={j.get('message')}\n")
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
    for pat in ("双语字幕版*.mp4", "*.mp4", "*.mkv"):
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


def _load_meta(meta_path: Path, video_path: Path, job: Path) -> dict:
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"读取 meta 失败：{e}\n")
            sys.exit(1)
    # Build a default meta next to the video, for the user to edit.
    # Pull title + source URL from the job manifest when present so a
    # copyright=2 (转载) submission has the required source link.
    m_title, m_source = _manifest_source(job)
    default = {
        "title": m_title or video_path.stem,
        "desc": "",
        "dynamic": "",
        "tag": "双语字幕,翻译",
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
        json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"已生成默认 publish-meta.json：{meta_path}\n"
        f"  请按需修改（尤其 copyright=2 时必须填 source 原视频来源链接）。"
    )
    return default


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
    video_path: Path, cover_path: Path | None, meta: dict, verify: Verify
) -> None:
    # 1) cover
    cover_url = ""
    if cover_path and cover_path.exists():
        print("上传封面…")
        try:
            cover_url = video.video_cover_upload(str(cover_path), verify)
            print(f"  封面 URL：{cover_url}")
        except Exception as e:
            sys.stderr.write(f"  封面上传失败（将不设置封面）：{e}\n")
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
            if isinstance(resp, dict) and resp.get("code") == 0:
                d = resp.get("data") or {}
                print(
                    f"投稿成功：BV{d.get('bvid', '?')} (aid={d.get('aid', '?')})"
                )
                return
            sys.stderr.write(f"  投稿返回异常（第 {attempt} 次）：{resp}\n")
        except Exception as e:
            sys.stderr.write(f"  投稿失败（第 {attempt} 次）：{e}\n")
        if attempt < 3:
            time.sleep(3 * attempt)
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
    meta = _load_meta(meta_path, video_path, job)
    _validate_meta(meta, meta_path)

    # Dry-run is a pure offline preview; it never requires a live login.
    if args.dry_run:
        _dry_run(video_path, cover_path, meta)
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
    _upload_and_submit(video_path, cover_path, meta, verify)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv) -> None:
    p = argparse.ArgumentParser(
        prog="publish_bilibili.py",
        description="Upload + submit a finished bilingual MP4 to Bilibili (opt-in).",
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
    pp.add_argument("--dry-run", action="store_true", help="validate + preview, no upload")

    args = p.parse_args(argv)
    {"login": cmd_login, "status": cmd_status, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    main(sys.argv[1:])
