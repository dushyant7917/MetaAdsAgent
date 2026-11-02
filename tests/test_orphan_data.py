import pytest

from metaads import jobrunner
from metaads.orphan_data import OrphanCleaner, run_job
from test_diskfiles import DAY, HOUR, NOW, Disk, add_job, disk, make_file  # noqa: F401  (disk is a fixture)


class Notes:
    def __init__(self):
        self.texts: list[str] = []

    def send_report(self, chat_id, text):
        self.texts.append(text)


@pytest.fixture
def pings(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(jobrunner, "ping", lambda url, suffix="": calls.append(suffix))
    return calls


def go(d, apply, now=NOW):
    notes = Notes()
    return run_job(d.s, notes, d.store, apply, "https://hc-ping.com/x", clock=lambda: now), notes


def layout(d):
    """One ad that is in the database (all its files linked) and a set of things no ad points to."""
    add_job(d, "b1-1")
    old = {"work orphan": make_file(d.path("work_dir", "b9-9", "image.png"), 1000),
           "adhoc": make_file(d.path("work_dir", "adhoc", "20260101_x.mp4"), 500),
           "part file": make_file(d.path("ads_dir", "hi", "ad_x.mp4.part"), 300),
           "manual hook": make_file(d.path("hooks_dir", "ta", "hook_20260101-000000.mp4"), 200),
           "unlinked sidecar": make_file(d.path("ads_dir", "hi", "ad_gone.json"), 5)}
    return old


def test_only_old_files_no_ad_points_to_are_deleted(pings, disk):
    orphans = layout(disk)
    young = make_file(disk.path("work_dir", "b8-8", "image.png"), 10, age_hours=1)       # a job may be writing it now
    body = make_file(disk.path("body_dir", "hi", "navy.mp4"), 77)
    poster = make_file(disk.path("poster_dir", "hi", "poster_demo.mp4"), 88)
    code, notes = go(disk, apply=True)
    assert code == 0 and pings == ["/start", ""]
    assert not any(p.exists() for p in orphans.values())
    assert not disk.path("work_dir", "b9-9").exists() and not disk.path("work_dir", "adhoc").exists()
    for kept in (disk.path("work_dir", "b1-1", "image.png"), disk.path("ads_dir", "hi", "ad_b1-1.mp4"),
                 disk.path("ads_dir", "hi", "ad_b1-1.json"), disk.path("hooks_dir", "hi", "hook_b1-1.mp4"),
                 disk.path("hooks_dir", "hi", "hook_b1-1.json"), young, body, poster):
        assert kept.exists(), kept
    text = notes.texts[0]
    assert text.startswith("🧹 Orphan data cleanup: 5 items deleted, 0.0 MB freed\n\n1. work: 2 items")
    assert "ads/hi: 2 items" in text and "hooks/ta: 1 item" in text and "b9-9" in text


def test_a_second_run_finds_nothing_and_stays_quiet(pings, disk):
    layout(disk)
    go(disk, apply=True)
    code, notes = go(disk, apply=True)
    assert code == 0 and notes.texts == [] and pings == ["/start", "", "/start", ""]


def test_a_dry_run_deletes_nothing_but_reports(pings, disk):
    orphans = layout(disk)
    code, notes = go(disk, apply=False)
    assert code == 0 and all(p.exists() for p in orphans.values())
    assert notes.texts[0].startswith("🧹 Orphan data cleanup DRY RUN: 5 items would be deleted")


def test_a_folder_is_as_new_as_the_newest_thing_inside_it(pings, disk):
    add_job(disk, "b1-1")
    make_file(disk.path("work_dir", "adhoc", "old.mp4"), 5, age_hours=500)
    make_file(disk.path("work_dir", "adhoc", "new.mp4"), 5, age_hours=3)
    go(disk, apply=True)
    assert disk.path("work_dir", "adhoc", "old.mp4").exists()
    go(disk, apply=True, now=NOW + 30 * HOUR)
    assert not disk.path("work_dir", "adhoc").exists()


def test_the_minimum_age_comes_from_the_settings(tmp_path, pings):
    d = Disk(tmp_path, orphan_data={"min_age_hours": 200})
    add_job(d, "b1-1")
    stray = make_file(d.path("ads_dir", "hi", "stray.mp4"), 5, age_hours=150)
    go(d, apply=True)
    assert stray.exists()
    go(d, apply=True, now=NOW + 60 * HOUR)
    assert not stray.exists()


def test_an_empty_jobs_table_deletes_nothing_and_fails_the_run(pings, disk):
    stray = make_file(disk.path("ads_dir", "hi", "stray.mp4"), 5)
    code, notes = go(disk, apply=True)
    assert code == 1 and stray.exists() and pings == ["/start", "/fail"]
    assert "jobs table is empty" in notes.texts[0]


def test_a_missing_drive_deletes_nothing_even_in_the_other_folders(pings, disk):
    add_job(disk, "b1-1")
    stray = make_file(disk.path("work_dir", "b9-9", "image.png"), 5)
    disk.path("hooks_dir", "hi", "hook_b1-1.json").unlink()
    for child in disk.path("hooks_dir").iterdir():
        for f in child.iterdir():
            f.unlink()
        child.rmdir()
    disk.path("hooks_dir").rmdir()                                       # as if the mount were gone
    code, notes = go(disk, apply=True)
    assert code == 1 and stray.exists() and "hooks_dir not found" in notes.texts[0]


def test_a_language_folder_that_does_not_exist_yet_is_fine(pings, disk):
    add_job(disk, "b1-1")
    disk.path("ads_dir", "ta").rmdir()
    assert go(disk, apply=True)[0] == 0


def test_a_failed_delete_is_reported_and_fails_the_run(pings, disk, monkeypatch):
    from metaads import orphan_data
    layout(disk)
    real = orphan_data.remove

    def flaky(settings, path):
        if path.name == "adhoc":
            raise PermissionError("denied")
        return real(settings, path)

    monkeypatch.setattr(orphan_data, "remove", flaky)
    code, notes = go(disk, apply=True)
    assert code == 1 and pings[-1] == "/fail"
    assert "could not delete" in notes.texts[0] and disk.path("work_dir", "adhoc").exists()
    assert not disk.path("work_dir", "b9-9").exists()                    # the rest was still deleted


def test_a_link_to_a_body_video_in_ads_dir_goes_but_the_video_stays(pings, disk):
    add_job(disk, "b1-1")
    body = make_file(disk.path("body_dir", "hi", "navy.mp4"), 77)
    link = disk.path("ads_dir", "hi", "link.mp4")
    link.symlink_to(body)
    import os
    os.utime(link, (NOW - 100 * HOUR, NOW - 100 * HOUR), follow_symlinks=False)
    go(disk, apply=True)
    assert not link.is_symlink() and body.exists()


def test_orphan_cleaner_counts_what_it_looked_at(disk):
    layout(disk)
    result = OrphanCleaner(disk.s, disk.store, lambda: NOW).run(apply=False)
    assert len(result.orphans) == 5 and result.deleted == [] and result.checked >= 8 and result.errors == []
