import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uploader as up  # noqa: E402

NOW = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)


def make_cfg(tmp_path, **defaults):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"stable_seconds": 5, "defaults": defaults}), encoding="utf-8")
    cfg = up.load_config(cfg_file)
    for d in (cfg.watch_dir, cfg.uploaded_dir, cfg.failed_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def put(path: Path, data=b"x", age=60):
    path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data, encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def test_config_paths_relative_to_config_file(tmp_path):
    cfg = make_cfg(tmp_path, tags=["a"])
    assert cfg.watch_dir == tmp_path / "inbox"
    assert cfg.defaults["tags"] == ["a"]
    assert cfg.defaults["privacyStatus"] == "private"  # 기본값 유지


def test_publish_at_utc_and_offset():
    assert up.parse_publish_at("2026-11-10T18:00:00+09:00", NOW) == "2026-11-10T09:00:00Z"
    assert up.parse_publish_at("2026-11-10T09:00:00Z", NOW) == "2026-11-10T09:00:00Z"
    assert up.parse_publish_at("2026-11-10 18:00+09:00", NOW) == "2026-11-10T09:00:00Z"


def test_publish_at_past_and_garbage():
    with pytest.raises(ValueError, match="지난"):
        up.parse_publish_at("2020-01-01T00:00:00Z", NOW)
    with pytest.raises(ValueError, match="형식"):
        up.parse_publish_at("내일 저녁", NOW)


def test_metadata_defaults_and_sidecar(tmp_path):
    v = put(tmp_path / "clip.mp4")
    meta = up.load_metadata(v, {"tags": ["x"], "privacyStatus": "public"}, NOW)
    assert meta["title"] == "clip" and meta["tags"] == ["x"] and meta["privacyStatus"] == "public"

    put(tmp_path / "clip.json", json.dumps({
        "title": "제목", "tags": "a, b ,", "privacyStatus": "public",
        "publishAt": "2026-12-01T10:00:00+09:00"}))
    meta = up.load_metadata(v, {}, NOW)
    assert meta["tags"] == ["a", "b"]
    assert meta["privacyStatus"] == "private"  # 예약 공개 → 강제 private
    body = up.build_body(meta)
    assert body["status"]["publishAt"] == "2026-12-01T01:00:00Z"
    assert body["snippet"]["title"] == "제목"


@pytest.mark.parametrize("bad, msg", [
    ({"title": "a" * 101}, "100자"),
    ({"title": "<b>"}, "< 또는 >"),
    ({"privacyStatus": "friends"}, "privacyStatus"),
    ({"tags": ["t" * 501]}, "태그"),
])
def test_metadata_validation(tmp_path, bad, msg):
    v = put(tmp_path / "clip.mp4")
    put(tmp_path / "clip.json", json.dumps(bad))
    with pytest.raises(ValueError, match=msg):
        up.load_metadata(v, {}, NOW)


def test_pending_skips_files_still_copying(tmp_path):
    put(tmp_path / "done.mp4")
    put(tmp_path / "copying.mp4", age=0)
    put(tmp_path / "empty.mp4", b"")
    put(tmp_path / "notes.txt")
    put(tmp_path / "meta_busy.mp4")
    put(tmp_path / "meta_busy.json", "{}", age=0)
    names = [p.name for p in up.pending_videos(tmp_path, 5)]
    assert names == ["done.mp4"]


def test_run_once_moves_files_and_records(tmp_path):
    cfg = make_cfg(tmp_path)
    put(cfg.watch_dir / "a.mp4")
    put(cfg.watch_dir / "a.json", json.dumps({"title": "A"}))
    put(cfg.watch_dir / "a.jpg")
    put(cfg.watch_dir / "b.mp4")
    put(cfg.watch_dir / "b.json", "{broken")

    calls = []

    def fake(video, meta, thumb):
        calls.append((video.name, meta["title"], thumb.name if thumb else None))
        return "VID123"

    stats = up.run_once(cfg, fake)
    assert calls == [("a.mp4", "A", "a.jpg")]
    assert stats == {"uploaded": 1, "failed": 1, "checked": 0, "deferred": 0}
    assert sorted(p.name for p in cfg.uploaded_dir.iterdir()) == ["a.jpg", "a.json", "a.mp4", "a.result.json"]
    rec = json.loads((cfg.uploaded_dir / "a.result.json").read_text(encoding="utf-8"))
    assert rec["url"] == "https://youtu.be/VID123"
    assert sorted(p.name for p in cfg.failed_dir.iterdir()) == ["b.error.json", "b.json", "b.mp4"]
    assert list(cfg.watch_dir.iterdir()) == []


def test_run_once_upload_error_goes_to_failed(tmp_path):
    cfg = make_cfg(tmp_path)
    put(cfg.watch_dir / "a.mp4")

    def boom(*_):
        raise RuntimeError("network down")

    stats = up.run_once(cfg, boom)
    assert stats["failed"] == 1
    err = json.loads((cfg.failed_dir / "a.error.json").read_text(encoding="utf-8"))
    assert "network down" in err["error"]


def test_quota_leaves_files_in_inbox(tmp_path):
    cfg = make_cfg(tmp_path)
    put(cfg.watch_dir / "a.mp4")
    put(cfg.watch_dir / "b.mp4")

    def quota(*_):
        raise up.QuotaExceeded("quotaExceeded")

    stats = up.run_once(cfg, quota)
    assert stats["deferred"] == 2 and stats["failed"] == 0
    assert sorted(p.name for p in cfg.watch_dir.iterdir()) == ["a.mp4", "b.mp4"]


def test_dry_run_changes_nothing(tmp_path):
    cfg = make_cfg(tmp_path)
    put(cfg.watch_dir / "a.mp4", age=0)  # 검사 모드는 복사 중 여부를 따지지 않음
    stats = up.run_once(cfg, None, dry_run=True)
    assert stats["checked"] == 1
    assert [p.name for p in cfg.watch_dir.iterdir()] == ["a.mp4"]


def test_name_collision_in_uploaded(tmp_path):
    cfg = make_cfg(tmp_path)
    put(cfg.uploaded_dir / "a.mp4")
    put(cfg.watch_dir / "a.mp4")
    up.run_once(cfg, lambda *_: "V")
    assert (cfg.uploaded_dir / "a_1.mp4").exists()


# ---- 재개 가능 업로드(resumable) 재시도 로직

class FakeResp(dict):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = "x"


def http_error(status, reason=""):
    from googleapiclient.errors import HttpError
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return HttpError(FakeResp(status), content)


class FakeRequest:
    def __init__(self, steps):
        self.steps = list(steps)

    def next_chunk(self):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_resumable_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    req = FakeRequest([http_error(503), ConnectionResetError(), (None, {"id": "OK"})])
    assert up._resumable(req, "v") == {"id": "OK"}


def test_resumable_quota_and_fatal(monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    with pytest.raises(up.QuotaExceeded):
        up._resumable(FakeRequest([http_error(403, "quotaExceeded")]), "v")
    from googleapiclient.errors import HttpError
    with pytest.raises(HttpError):
        up._resumable(FakeRequest([http_error(400, "invalidTitle")]), "v")


def test_resumable_gives_up(monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    req = FakeRequest([http_error(500)] * (up.MAX_RETRIES + 1))
    with pytest.raises(RuntimeError, match="재시도"):
        up._resumable(req, "v")


# ---- Google Drive 모드

def test_folder_id_from_links():
    fid = "1AbCdEfGhIjKlMnOpQrStUv"
    assert up.folder_id(f"https://drive.google.com/drive/folders/{fid}?usp=sharing") == fid
    assert up.folder_id(f"https://drive.google.com/drive/u/0/folders/{fid}") == fid
    assert up.folder_id(f"https://drive.google.com/open?id={fid}") == fid
    assert up.folder_id(f"  {fid} ") == fid


def test_plan_drive_batch_groups_companions_and_limits():
    files = [
        {"id": "1", "name": "b.mp4", "mimeType": "video/mp4"},
        {"id": "2", "name": "a.mov", "mimeType": "video/quicktime"},
        {"id": "3", "name": "a.json", "mimeType": "application/json"},
        {"id": "4", "name": "a.JPG", "mimeType": "image/jpeg"},
        {"id": "5", "name": "a.txt", "mimeType": "text/plain"},
        {"id": "6", "name": "c.mp4", "mimeType": "video/mp4"},
        {"id": "7", "name": "메모", "mimeType": "application/vnd.google-apps.document"},
        {"id": "8", "name": "b.mp4", "mimeType": "video/mp4"},  # 중복 이름
    ]
    batch = up.plan_drive_batch(files, max_videos=2)
    assert [(v["name"], sorted(c["name"] for c in cs)) for v, cs in batch] == [
        ("a.mov", ["a.JPG", "a.json"]),
        ("b.mp4", []),
    ]


class FakeDrive:
    """drive_* 함수를 바꿔 끼워 Drive를 흉내 냅니다."""

    def __init__(self, files):
        self.files = {f["id"]: dict(f, parent="IN") for f in files}
        self.put = []

    def install(self, monkeypatch):
        monkeypatch.setattr(up, "drive_list", lambda d, parent: [f for f in self.files.values() if f["parent"] == parent])
        monkeypatch.setattr(up, "drive_download", lambda d, fid, dest: dest.write_bytes(self.files[fid]["data"]))

        def move(d, fid, src, dst):
            assert self.files[fid]["parent"] == src
            self.files[fid]["parent"] = dst
        monkeypatch.setattr(up, "drive_move", move)
        monkeypatch.setattr(up, "drive_put", lambda d, path, parent: self.put.append(
            (path.name, parent, json.loads(path.read_text(encoding="utf-8")))))
        monkeypatch.setattr(up, "drive_export_csv", lambda d, fid: self.files[fid]["data"].decode("utf-8"))

    def where(self):
        return {f["name"]: f["parent"] for f in self.files.values()}


def test_run_drive_moves_results_and_keeps_deferred(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake = FakeDrive([
        {"id": "1", "name": "a.mp4", "data": b"video-a"},
        {"id": "2", "name": "a.json", "data": json.dumps({"title": "에이"}).encode()},
        {"id": "3", "name": "a.png", "data": b"png"},
        {"id": "4", "name": "b.mp4", "data": b"video-b"},
        {"id": "5", "name": "b.json", "data": b"{broken"},
        {"id": "6", "name": "c.mp4", "data": b"video-c"},
    ])
    fake.install(monkeypatch)
    seen = []

    def upload(video, meta, thumb):
        if video.name == "c.mp4":
            raise up.QuotaExceeded("quotaExceeded")
        seen.append((video.read_bytes(), meta["title"], thumb.name if thumb else None))
        return "VIDA"

    stats = up.run_drive(cfg, upload, None, "IN", "DONE", "FAIL", max_videos=5)
    assert stats["uploaded"] == 1 and stats["failed"] == 1 and stats["deferred"] == 1
    assert seen == [(b"video-a", "에이", "a.png")]
    assert fake.where() == {"a.mp4": "DONE", "a.json": "DONE", "a.png": "DONE",
                            "b.mp4": "FAIL", "b.json": "FAIL", "c.mp4": "IN"}
    names = {(n, p) for n, p, _ in fake.put}
    assert names == {("a.result.json", "DONE"), ("b.error.json", "FAIL")}
    result = next(d for n, _, d in fake.put if n == "a.result.json")
    assert result["url"] == "https://youtu.be/VIDA"
    # 로컬 작업 폴더는 쓰지 않음
    assert list(cfg.watch_dir.iterdir()) == []


def test_run_drive_empty(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    FakeDrive([]).install(monkeypatch)
    assert up.run_drive(cfg, None, None, "IN", "DONE", "FAIL", 3)["uploaded"] == 0


def test_env_credentials_require_client(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_REFRESH_TOKEN", "r")
    monkeypatch.delenv("YT_CLIENT_ID", raising=False)
    monkeypatch.setenv("YT_CLIENT_SECRET", "s")
    with pytest.raises(SystemExit, match="YT_CLIENT_ID"):
        up.get_credentials(make_cfg(tmp_path), up.SCOPES)


def test_drive_cli_requires_distinct_folders(monkeypatch):
    for k in ("DRIVE_INBOX_FOLDER", "DRIVE_UPLOADED_FOLDER", "DRIVE_FAILED_FOLDER"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="--failed"):
        up.main(["drive", "--inbox", "A", "--uploaded", "B"])
    with pytest.raises(SystemExit, match="서로 다른"):
        up.main(["drive", "--inbox", "A", "--uploaded", "A", "--failed", "C"])


SHEET_CSV = """\ufeff파일,제목,설명,태그,공개,예약,아동용
IMG_0001.MOV,여행 브이로그,"첫 줄
둘째 줄","여행, 브이로그",공개,2099-01-01 18:00,아니오
img_0002,두번째,,,일부 공개,,
,빈 파일명,,,,,
IMG_0003.MOV,,,,,,
"""


def test_parse_sheet_csv_korean_headers():
    rows = up.parse_sheet_csv(SHEET_CSV)
    assert set(rows) == {"img_0001.mov", "img_0002"}
    assert rows["img_0001.mov"]["description"] == "첫 줄\n둘째 줄"
    assert up.sheet_meta_for(rows, "IMG_0002.mp4")["title"] == "두번째"
    assert up.sheet_meta_for(rows, "IMG_0003.MOV") is None
    assert up.parse_sheet_csv("title,desc\na,b\n") == {}  # 파일 열 없음


def test_sheet_values_become_metadata(tmp_path):
    rows = up.parse_sheet_csv(SHEET_CSV)
    v = put(tmp_path / "IMG_0001.MOV")
    put(tmp_path / "IMG_0001.json", json.dumps(rows["img_0001.mov"], ensure_ascii=False))
    meta = up.load_metadata(v, {}, NOW)
    assert meta["tags"] == ["여행", "브이로그"]
    assert meta["privacyStatus"] == "private"  # 예약 공개라서
    assert meta["madeForKids"] is False
    v2 = put(tmp_path / "IMG_0002.mp4")
    put(tmp_path / "IMG_0002.json", json.dumps(rows["img_0002"], ensure_ascii=False))
    assert up.load_metadata(v2, {}, NOW)["privacyStatus"] == "unlisted"


def test_run_drive_uses_sheet_and_json_overrides(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    fake = FakeDrive([
        {"id": "s", "name": "업로드 목록", "mimeType": up.SHEET_MIME, "data": SHEET_CSV.encode()},
        {"id": "1", "name": "IMG_0001.MOV", "data": b"v1"},
        {"id": "2", "name": "IMG_0002.mp4", "data": b"v2"},
        {"id": "3", "name": "IMG_0002.json", "data": json.dumps({"title": "json 우선"}).encode()},
    ])
    fake.install(monkeypatch)
    titles = {}

    def upload(video, meta, thumb):
        titles[video.name] = (meta["title"], meta["privacyStatus"])
        return "V"

    stats = up.run_drive(cfg, upload, None, "IN", "DONE", "FAIL", 5)
    assert stats["uploaded"] == 2
    assert titles == {"IMG_0001.MOV": ("여행 브이로그", "private"), "IMG_0002.mp4": ("json 우선", "unlisted")}
    assert fake.where()["업로드 목록"] == "IN"  # 시트는 그대로 남음
