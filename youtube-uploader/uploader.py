#!/usr/bin/env python3
"""YouTube 자동 업로더.

감시 폴더(inbox)에 영상 파일이 들어오면 YouTube에 업로드합니다.
같은 이름의 .json 파일로 제목, 설명, 태그, 예약 공개 시간 등을 지정할 수 있습니다.

    python uploader.py auth     # 최초 1회 Google 계정 인증
    python uploader.py check    # 업로드 없이 메타데이터만 검사
    python uploader.py once     # 대기 중인 영상을 한 번 업로드
    python uploader.py watch    # 폴더를 계속 감시하며 업로드
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("uploader")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg"}
THUMB_EXTS = (".jpg", ".jpeg", ".png")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # 재생목록 추가에 필요
]
PRIVACY_VALUES = {"private", "unlisted", "public"}
RETRIABLE_STATUS = {500, 502, 503, 504}
QUOTA_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
MAX_RETRIES = 8
CHUNK_SIZE = 8 * 1024 * 1024

DEFAULT_CONFIG = {
    "watch_dir": "inbox",
    "uploaded_dir": "uploaded",
    "failed_dir": "failed",
    "client_secrets": "client_secret.json",
    "token_file": "token.json",
    "poll_seconds": 30,
    "stable_seconds": 10,
    "defaults": {
        "description": "",
        "tags": [],
        "categoryId": "22",
        "privacyStatus": "private",
        "madeForKids": False,
    },
}


class QuotaExceeded(Exception):
    """API 할당량 초과. 파일은 그대로 두고 다음에 다시 시도합니다."""


@dataclass
class Config:
    base_dir: Path
    watch_dir: Path
    uploaded_dir: Path
    failed_dir: Path
    client_secrets: Path
    token_file: Path
    poll_seconds: float
    stable_seconds: float
    defaults: dict = field(default_factory=dict)


def load_config(path: Path | None) -> Config:
    raw = json.loads(json.dumps(DEFAULT_CONFIG))  # 깊은 복사
    base = Path.cwd()
    if path is not None:
        path = path.resolve()
        if path.exists():
            user = json.loads(path.read_text(encoding="utf-8"))
            raw["defaults"].update(user.pop("defaults", {}) or {})
            raw.update(user)
            base = path.parent
        else:
            log.warning("설정 파일 %s 이(가) 없어 기본값을 사용합니다.", path)

    def p(key: str) -> Path:
        v = Path(raw[key]).expanduser()
        return v if v.is_absolute() else base / v

    return Config(
        base_dir=base,
        watch_dir=p("watch_dir"),
        uploaded_dir=p("uploaded_dir"),
        failed_dir=p("failed_dir"),
        client_secrets=p("client_secrets"),
        token_file=p("token_file"),
        poll_seconds=float(raw["poll_seconds"]),
        stable_seconds=float(raw["stable_seconds"]),
        defaults=raw["defaults"],
    )


# ---------------------------------------------------------------- 메타데이터

def parse_publish_at(value: str, now: datetime | None = None) -> str:
    """예약 공개 시간을 RFC 3339 UTC 문자열로 바꿉니다.

    시간대가 없으면 이 컴퓨터의 현지 시간으로 봅니다.
    """
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f"publishAt 형식이 잘못되었습니다: {value!r} (예: 2026-11-10 18:00)") from e
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 현지 시간으로 해석
    dt = dt.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if dt <= now:
        raise ValueError(f"publishAt {value!r} 은(는) 이미 지난 시간입니다.")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sidecar_path(video: Path) -> Path:
    return video.with_suffix(".json")


def load_metadata(video: Path, defaults: dict, now: datetime | None = None) -> dict:
    meta = dict(defaults)
    side = sidecar_path(video)
    if side.exists():
        try:
            meta.update(json.loads(side.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            raise ValueError(f"{side.name} JSON 파싱 실패: {e}") from e
    meta.setdefault("title", video.stem)
    if not meta.get("title"):
        meta["title"] = video.stem

    title = str(meta["title"]).strip()
    if len(title) > 100:
        raise ValueError(f"제목은 100자 이하여야 합니다 (현재 {len(title)}자).")
    if "<" in title or ">" in title:
        raise ValueError("제목에 < 또는 > 문자를 쓸 수 없습니다.")
    meta["title"] = title

    desc = str(meta.get("description") or "")
    if len(desc.encode("utf-8")) > 5000:
        raise ValueError("설명은 5000바이트 이하여야 합니다.")
    if "<" in desc or ">" in desc:
        raise ValueError("설명에 < 또는 > 문자를 쓸 수 없습니다.")
    meta["description"] = desc

    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [str(t).strip() for t in tags if str(t).strip()]
    # YouTube는 공백이 있는 태그를 따옴표 포함 길이로 셉니다.
    total = sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)
    if total > 500:
        raise ValueError(f"태그 전체 길이는 500자 이하여야 합니다 (현재 {total}자).")
    meta["tags"] = tags

    privacy = str(meta.get("privacyStatus") or "private").lower()
    if privacy not in PRIVACY_VALUES:
        raise ValueError(f"privacyStatus 는 {sorted(PRIVACY_VALUES)} 중 하나여야 합니다.")
    meta["privacyStatus"] = privacy

    if meta.get("publishAt"):
        meta["publishAt"] = parse_publish_at(str(meta["publishAt"]), now)
        if privacy != "private":
            log.info("%s: 예약 공개는 비공개(private) 상태여야 해서 privacyStatus를 private로 바꿉니다.", video.name)
        meta["privacyStatus"] = "private"
    else:
        meta.pop("publishAt", None)

    meta["categoryId"] = str(meta.get("categoryId") or "22")
    meta["madeForKids"] = bool(meta.get("madeForKids", False))
    return meta


def build_body(meta: dict) -> dict:
    snippet = {
        "title": meta["title"],
        "description": meta["description"],
        "tags": meta["tags"],
        "categoryId": meta["categoryId"],
    }
    for key in ("defaultLanguage", "defaultAudioLanguage"):
        if meta.get(key):
            snippet[key] = meta[key]
    status = {
        "privacyStatus": meta["privacyStatus"],
        "selfDeclaredMadeForKids": meta["madeForKids"],
    }
    if meta.get("publishAt"):
        status["publishAt"] = meta["publishAt"]
    return {"snippet": snippet, "status": status}


def find_thumbnail(video: Path, meta: dict) -> Path | None:
    if meta.get("thumbnail"):
        t = Path(meta["thumbnail"]).expanduser()
        t = t if t.is_absolute() else video.parent / t
        if not t.exists():
            raise ValueError(f"썸네일 파일이 없습니다: {t}")
        return t
    for ext in THUMB_EXTS:
        for cand in (video.with_suffix(ext), video.with_suffix(ext.upper())):
            if cand.exists():
                return cand
    return None


def companion_files(video: Path, meta: dict | None = None) -> list[Path]:
    files = [video]
    side = sidecar_path(video)
    if side.exists():
        files.append(side)
    try:
        thumb = find_thumbnail(video, meta or {})
    except ValueError:
        thumb = None
    if thumb and thumb.parent == video.parent:
        files.append(thumb)
    return files


# ---------------------------------------------------------------- 폴더 처리

def is_stable(path: Path, stable_seconds: float, now: float | None = None) -> bool:
    """파일 복사가 끝났는지(일정 시간 수정이 없었는지) 확인합니다."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    now = time.time() if now is None else now
    return st.st_size > 0 and (now - st.st_mtime) >= stable_seconds


def pending_videos(watch_dir: Path, stable_seconds: float, now: float | None = None) -> list[Path]:
    if not watch_dir.exists():
        return []
    out = []
    for f in sorted(watch_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS and not f.name.startswith("."):
            side = sidecar_path(f)
            if is_stable(f, stable_seconds, now) and (not side.exists() or is_stable(side, stable_seconds, now)):
                out.append(f)
    return out


def move_unique(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}_{n}{src.suffix}"
        n += 1
    shutil.move(str(src), str(dest))
    return dest


def _record(dest_dir: Path, video: Path, data: dict, suffix: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    rec = dest_dir / f"{video.stem}{suffix}"
    n = 1
    while rec.exists():
        rec = dest_dir / f"{video.stem}_{n}{suffix}"
        n += 1
    rec.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


UploadFn = Callable[[Path, dict, "Path | None"], str]


def run_once(cfg: Config, upload_fn: UploadFn | None, dry_run: bool = False) -> dict:
    """대기 중인 영상을 처리합니다. 결과 개수를 돌려줍니다."""
    stats = {"uploaded": 0, "failed": 0, "checked": 0, "deferred": 0}
    videos = pending_videos(cfg.watch_dir, 0 if dry_run else cfg.stable_seconds)
    for video in videos:
        try:
            meta = load_metadata(video, cfg.defaults)
            thumb = find_thumbnail(video, meta)
        except ValueError as e:
            log.error("%s: %s", video.name, e)
            if not dry_run:
                _fail(cfg, video, str(e))
            stats["failed"] += 1
            continue

        if dry_run:
            log.info("[검사] %s\n%s\n  썸네일: %s  재생목록: %s",
                     video.name, json.dumps(build_body(meta), ensure_ascii=False, indent=2),
                     thumb.name if thumb else "없음", meta.get("playlistId") or "없음")
            stats["checked"] += 1
            continue

        assert upload_fn is not None
        log.info("업로드 시작: %s (%s)", video.name, meta["title"])
        try:
            video_id = upload_fn(video, meta, thumb)
        except QuotaExceeded as e:
            log.error("할당량 초과로 이번 업로드를 멈춥니다 (파일은 inbox에 그대로 둡니다): %s", e)
            stats["deferred"] += len(videos) - stats["uploaded"] - stats["failed"]
            break
        except Exception as e:  # noqa: BLE001 - 어떤 오류든 실패 폴더로 보냅니다
            log.exception("%s 업로드 실패", video.name)
            _fail(cfg, video, f"{type(e).__name__}: {e}", meta)
            stats["failed"] += 1
            continue

        url = f"https://youtu.be/{video_id}"
        files = companion_files(video, meta)
        for f in files:
            move_unique(f, cfg.uploaded_dir)
        _record(cfg.uploaded_dir, video, {
            "videoId": video_id,
            "url": url,
            "title": meta["title"],
            "privacyStatus": meta["privacyStatus"],
            "publishAt": meta.get("publishAt"),
            "uploadedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, ".result.json")
        log.info("업로드 완료: %s -> %s%s", video.name, url,
                 f" (예약 공개 {meta['publishAt']})" if meta.get("publishAt") else "")
        stats["uploaded"] += 1
    return stats


def _fail(cfg: Config, video: Path, reason: str, meta: dict | None = None) -> None:
    for f in companion_files(video, meta):
        move_unique(f, cfg.failed_dir)
    _record(cfg.failed_dir, video, {"error": reason}, ".error.json")


# ---------------------------------------------------------------- YouTube API

def get_service(cfg: Config, open_browser: bool = True):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if cfg.token_file.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not cfg.client_secrets.exists():
                raise SystemExit(
                    f"OAuth 클라이언트 파일이 없습니다: {cfg.client_secrets}\n"
                    "README의 'Google Cloud 설정'을 따라 client_secret.json을 받아 두세요.")
            flow = InstalledAppFlow.from_client_secrets_file(str(cfg.client_secrets), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=open_browser)
        cfg.token_file.write_text(creds.to_json(), encoding="utf-8")
        try:
            cfg.token_file.chmod(0o600)
        except OSError:
            pass
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _http_reason(err) -> str:
    try:
        data = json.loads(err.content.decode("utf-8"))
        return data["error"]["errors"][0]["reason"]
    except Exception:  # noqa: BLE001
        return ""


def _resumable(request, label: str):
    import httplib2
    from googleapiclient.errors import HttpError

    response, retry = None, 0
    while response is None:
        error = None
        try:
            status, response = request.next_chunk()
            if status:
                log.info("  %s: %d%%", label, int(status.progress() * 100))
        except HttpError as e:
            if e.resp.status == 403 and _http_reason(e) in QUOTA_REASONS:
                raise QuotaExceeded(_http_reason(e)) from e
            if e.resp.status not in RETRIABLE_STATUS:
                raise
            error = e
        except (OSError, httplib2.HttpLib2Error) as e:
            error = e
        if error is not None:
            retry += 1
            if retry > MAX_RETRIES:
                raise RuntimeError(f"재시도 {MAX_RETRIES}회 후에도 실패: {error}") from error
            wait = random.random() * (2 ** retry)
            log.warning("  일시 오류(%s), %.1f초 후 재시도 (%d/%d)", error, wait, retry, MAX_RETRIES)
            time.sleep(wait)
    return response


def make_youtube_uploader(youtube) -> UploadFn:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    def upload(video: Path, meta: dict, thumb: Path | None) -> str:
        media = MediaFileUpload(str(video), chunksize=CHUNK_SIZE, resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=build_body(meta), media_body=media,
                                      notifySubscribers=bool(meta.get("notifySubscribers", True)))
        resp = _resumable(req, video.name)
        video_id = resp.get("id")
        if not video_id:
            raise RuntimeError(f"예상치 못한 응답: {resp}")

        if thumb:
            try:
                youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumb))).execute()
                log.info("  썸네일 설정: %s", thumb.name)
            except HttpError as e:
                log.warning("  썸네일 설정 실패 (채널 인증이 필요할 수 있음): %s", e)

        if meta.get("playlistId"):
            try:
                youtube.playlistItems().insert(part="snippet", body={"snippet": {
                    "playlistId": meta["playlistId"],
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }}).execute()
                log.info("  재생목록 추가: %s", meta["playlistId"])
            except HttpError as e:
                log.warning("  재생목록 추가 실패: %s", e)
        return video_id

    return upload


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="감시 폴더의 영상을 YouTube에 자동 업로드합니다.")
    ap.add_argument("command", choices=["auth", "check", "once", "watch"])
    ap.add_argument("-c", "--config", type=Path, default=Path("config.json"), help="설정 파일 (기본: config.json)")
    ap.add_argument("--no-browser", action="store_true", help="인증 시 브라우저를 자동으로 열지 않음")
    ap.add_argument("--log-file", type=Path, help="로그를 파일에도 기록")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    cfg = load_config(args.config)
    for d in (cfg.watch_dir, cfg.uploaded_dir, cfg.failed_dir):
        d.mkdir(parents=True, exist_ok=True)

    if args.command == "check":
        stats = run_once(cfg, None, dry_run=True)
        log.info("검사 완료: 정상 %d개, 오류 %d개", stats["checked"], stats["failed"])
        return 1 if stats["failed"] else 0

    youtube = get_service(cfg, open_browser=not args.no_browser)
    if args.command == "auth":
        log.info("인증 완료. 토큰을 %s 에 저장했습니다.", cfg.token_file)
        return 0

    upload_fn = make_youtube_uploader(youtube)
    if args.command == "once":
        stats = run_once(cfg, upload_fn)
        log.info("완료: 업로드 %d, 실패 %d, 보류 %d", stats["uploaded"], stats["failed"], stats["deferred"])
        return 1 if stats["failed"] else 0

    log.info("%s 폴더 감시 시작 (%d초 간격). 종료하려면 Ctrl+C", cfg.watch_dir, cfg.poll_seconds)
    try:
        while True:
            stats = run_once(cfg, upload_fn)
            # 할당량 초과 시 1시간 쉬었다가 다시 시도
            time.sleep(3600 if stats["deferred"] else cfg.poll_seconds)
    except KeyboardInterrupt:
        log.info("종료합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
