"""The files an ad leaves on disk and the one guarded delete both cleanup jobs use. The fixtures here (`disk`, `add_job`,
`make_file`) are reused by test_orphan_data.py and test_old_data.py."""
import copy
import os
from contextlib import closing
from pathlib import Path

import pytest

from metaads.config import Settings, load_settings
from metaads.diskfiles import Deleted, Refused, job_files, newest_change, path_key, remove, report_text, size_of
from metaads.jobs import JobStore

BASE = load_settings()
NOW = 1_800_000_000.0
HOUR = 3600
DAY = 24 * HOUR


class Disk:
    """Settings whose folders are all under tmp_path, and a jobs database there."""

    def __init__(self, tmp_path: Path, **sections):
        d = copy.deepcopy(BASE.to_dict())
        for name in ("body_dir", "poster_dir", "hooks_dir", "ads_dir", "work_dir"):
            d["paths"][name] = str(tmp_path / name)
        d["paths"]["db"] = str(tmp_path / "db.sqlite")
        for name, values in sections.items():
            d[name].update(values)
        self.s, self.root = Settings(d), tmp_path
        self.store = JobStore(self.s.paths.db)
        for name in ("body_dir", "poster_dir", "hooks_dir", "ads_dir", "work_dir"):
            (tmp_path / name).mkdir()
        for lang in ("hi", "ta"):
            for name in ("hooks_dir", "ads_dir", "body_dir", "poster_dir"):
                (tmp_path / name / lang).mkdir()

    def path(self, setting: str, *parts: str) -> Path:
        return Path(self.s.paths[setting]).joinpath(*parts)


@pytest.fixture
def disk(tmp_path):
    return Disk(tmp_path)


def make_file(path: Path, size: int = 10, age_hours: float = 100, now: float = NOW) -> Path:
    """A file of `size` bytes last changed `age_hours` before `now`; a folder is created with the file in it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = now - age_hours * HOUR
    os.utime(path, (stamp, stamp))
    return path


def add_job(d: Disk, job_id: str, *, status="done", review=None, reviewed=None, language="hi", ad_format="video",
            created=NOW - 10 * DAY, updated=None, upload=None, upload_updated=None, ad_id="ad1", with_files=True,
            hook: str | None = None, now: float = NOW) -> dict:
    """A job row in `d.store` with its files on disk (work folder with one file, ad and hook with sidecars).
    `upload` is the status of its Meta upload (None: never uploaded); jobs given the same `hook` share its texts."""
    ad = d.path("ads_dir", language, f"ad_{job_id}.mp4")
    hook_file = d.path("hooks_dir", language, f"hook_{job_id}.mp4") if ad_format == "video" else None
    with closing(d.store._conn()) as c, c:
        c.execute("INSERT INTO jobs (id,batch_id,idx,language,spoken_text,thumbnail_text,status,stage,hook_path,ad_path,"
                  "created,updated,review,reviewed,ad_format) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (job_id, "b", 0, language, f"say {hook or job_id}", f"thumb {hook or job_id}", status, status, str(hook_file) if hook_file else None,
                   str(ad), created, updated if updated is not None else created, review, reviewed, ad_format))
    if upload:
        d.store.upsert_meta_upload(job_id, status=upload, adset_id=f"as-{job_id}", ad_id=ad_id if upload == "done" else None)
        with closing(d.store._conn()) as c, c:
            c.execute("UPDATE meta_uploads SET updated=? WHERE job_id=?", (upload_updated, job_id))
    if with_files:
        make_file(d.path("work_dir", job_id, "image.png"), 100, now=now)
        make_file(d.path("work_dir", job_id, "ad.mp4"), 200, now=now)
        for published in (ad, hook_file):
            if published:
                make_file(published, 50, now=now)
                make_file(published.with_suffix(".json"), 5, now=now)
    return d.store.get_job(job_id)


def test_the_files_of_a_job_are_its_work_folder_ad_hook_and_their_sidecars(disk):
    job = add_job(disk, "b1-1")
    assert job_files(disk.s, job) == [disk.path("work_dir", "b1-1"),
                                      disk.path("ads_dir", "hi", "ad_b1-1.mp4"), disk.path("ads_dir", "hi", "ad_b1-1.json"),
                                      disk.path("hooks_dir", "hi", "hook_b1-1.mp4"), disk.path("hooks_dir", "hi", "hook_b1-1.json")]
    image = add_job(disk, "b1-2", ad_format="image")                    # an image ad has no hook file
    assert len(job_files(disk.s, image)) == 3


def test_remove_deletes_a_file_or_a_folder_and_reports_the_bytes_freed(disk):
    folder = disk.path("work_dir", "b1-1")
    make_file(folder / "image.png", 100)
    make_file(folder / "deeper" / "ad.mp4", 250)
    assert size_of(folder) == 350
    assert remove(disk.s, folder) == 350 and not folder.exists()
    assert remove(disk.s, folder) == 0                                    # already gone
    single = make_file(disk.path("ads_dir", "hi", "ad.mp4"), 40)
    assert remove(disk.s, single) == 40 and not single.exists()


@pytest.mark.parametrize("where", [
    ("body_dir", "hi", "navy.mp4"), ("poster_dir", "hi", "poster_demo.mp4"), ("body_dir",), ("poster_dir", "hi"),
])
def test_body_and_poster_videos_are_never_deleted(disk, where):
    target = disk.path(*where)
    target.parent.mkdir(parents=True, exist_ok=True)
    if len(where) == 3:
        make_file(target, 77)
    with pytest.raises(Refused, match="reuses"):
        remove(disk.s, target)
    assert target.exists()


def test_body_videos_survive_even_when_a_folder_setting_points_into_them(tmp_path):
    d = Disk(tmp_path)
    body = make_file(d.path("body_dir", "hi", "navy.mp4"), 77)
    d.s.to_dict()["paths"]["ads_dir"] = str(d.path("body_dir"))            # a mis-set ads_dir
    with pytest.raises(Refused):
        remove(d.s, body)
    assert body.exists()


@pytest.mark.parametrize("outside", ["elsewhere/file.mp4", "work_dir/../elsewhere/file.mp4"])
def test_nothing_outside_the_three_folders_is_deleted(disk, outside):
    stray = make_file(disk.root / outside, 5)
    with pytest.raises(Refused, match="outside"):
        remove(disk.s, stray)
    assert stray.exists()


@pytest.mark.parametrize("setting", ["work_dir", "ads_dir", "hooks_dir"])
def test_the_folders_themselves_are_not_deleted(disk, setting):
    with pytest.raises(Refused):
        remove(disk.s, disk.path(setting))
    assert disk.path(setting).is_dir()


def test_a_symlink_is_removed_but_never_followed(disk):
    video = make_file(disk.path("body_dir", "hi", "navy.mp4"), 77)
    link = disk.path("ads_dir", "hi", "link.mp4")
    link.symlink_to(video)
    folder_link = disk.path("work_dir", "linked")
    folder_link.symlink_to(disk.path("body_dir", "hi"))
    own_size = link.lstat().st_size
    assert remove(disk.s, link) == own_size                                # the link itself, not the file it points to
    assert not link.is_symlink() and video.exists()
    remove(disk.s, folder_link)
    assert not folder_link.is_symlink() and video.read_bytes() == b"x" * 77
    inner = disk.path("work_dir", "b1-1")
    make_file(inner / "a.png", 3)
    (inner / "to_body").symlink_to(disk.path("body_dir", "hi"))
    remove(disk.s, inner)                                                  # a link inside a folder is not followed either
    assert not inner.exists() and video.exists()


def test_a_symlink_is_judged_by_where_it_sits_not_by_what_it_points_to(disk):
    video = make_file(disk.path("body_dir", "hi", "navy.mp4"), 5000)
    link = disk.path("ads_dir", "hi", "link.mp4")
    link.symlink_to(video)
    assert size_of(link) == link.lstat().st_size and size_of(link) < 5000                                            # its own size, not the target's
    assert path_key(link) == disk.path("ads_dir", "hi").resolve() / "link.mp4"


def test_two_spellings_of_a_path_have_the_same_key(disk):
    assert path_key(disk.path("ads_dir", "hi", "..", "hi", "a.mp4")) == path_key(disk.path("ads_dir", "hi", "a.mp4"))


def test_newest_change_looks_inside_a_folder(disk):
    folder = disk.path("work_dir", "b1-1")
    make_file(folder / "old.png", 1, age_hours=200)
    make_file(folder / "new.png", 1, age_hours=2)
    os.utime(folder, (NOW - 300 * HOUR, NOW - 300 * HOUR))
    assert newest_change(folder) == NOW - 2 * HOUR


def test_a_quiet_report_is_none_a_dry_run_says_nothing_was_changed(disk):
    assert report_text("Cleanup", [], [], True, disk.s) is None
    items = [Deleted(disk.path("work_dir", f"b{i}"), 2 * 1024 * 1024, "work") for i in range(8)]
    items.append(Deleted(disk.path("ads_dir", "hi", "a.mp4"), 1024 * 1024, "ads/hi"))
    dry = report_text("Cleanup", items, [], False, disk.s)
    assert dry.startswith("🧹 Cleanup DRY RUN: 9 items would be deleted, 17.0 MB would be freed (nothing was changed)\n\n")
    live = report_text("Cleanup", items, ["could not delete x"], True, disk.s)
    assert live.startswith("🧹 Cleanup: 9 items deleted, 17.0 MB freed\n\n1. work: 8 items, 16.0 MB\n   b0\n")
    shown = disk.s.messages.cleanup_names_shown
    assert f"   and {8 - shown} more" in live and "\n\n2. ads/hi: 1 item, 1.0 MB\n   a.mp4" in live
    assert live.endswith("⚠️ could not delete x")
    assert report_text("Cleanup", [], ["boom"], True, disk.s) == "⚠️ boom"
