"""The one-off migration that turns the copies made by earlier versions into references."""
from contextlib import closing
from pathlib import Path

from metaads import hookfiles
from metaads.dedupe_files import Deduper, run_job
from test_diskfiles import NOW, add_job, disk, make_file  # noqa: F401  (disk is a fixture)

HOOK = hookfiles.HOOK_FILES


def put(disk, job_id, name, data: bytes) -> Path:
    path = disk.path("work_dir", job_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def hook_files(disk, job_id, **different):
    """The five hook files of a job; `different` gives other bytes for some stages."""
    for stage, name in HOOK.items():
        put(disk, job_id, name, different.get(stage, f"{stage}-data".encode()))


def redo(disk, child, parent):
    with closing(disk.store._conn()) as c, c:
        c.execute("UPDATE jobs SET redo_of=? WHERE id=?", (parent, child))


def run(disk, apply=True):
    return Deduper(disk.s, disk.store).run(apply)


def work_files(disk, job_id):
    return sorted(p.name for p in disk.path("work_dir", job_id).iterdir()) if disk.path("work_dir", job_id).exists() else []


def test_a_redo_that_copied_the_hook_files_points_to_the_ad_it_came_from(disk):
    add_job(disk, "p", with_files=False, created=NOW - 5)
    add_job(disk, "c", with_files=False, created=NOW - 4)
    hook_files(disk, "p")
    hook_files(disk, "c")
    redo(disk, "c", "p")
    result = run(disk)
    job = disk.store.get_job("c")
    assert (job["redo_of"], job["reused_stages"]) == ("p", 5) and result.linked == ["c"] and result.errors == []
    assert work_files(disk, "c") == [] and len(work_files(disk, "p")) == 5          # one copy of each file is left
    assert hookfiles.stage_file(disk.s, job, "render_hook", disk.store.get_job).read_bytes() == b"render_hook-data"


def test_only_the_leading_identical_stages_become_references(disk):
    add_job(disk, "p", with_files=False, created=NOW - 5)
    add_job(disk, "c", with_files=False, created=NOW - 4)
    hook_files(disk, "p")
    hook_files(disk, "c", video=b"a new Veo clip")                                   # the owner asked for a new clip
    redo(disk, "c", "p")
    run(disk)
    assert disk.store.get_job("c")["reused_stages"] == 1                             # only the image is shared
    assert work_files(disk, "c") == sorted(n for s, n in HOOK.items() if s != "image")


def test_the_chain_is_followed_and_a_copy_without_an_original_stays(disk):
    for job_id, created in (("a", 5), ("b", 4), ("c", 3), ("lone", 2)):
        add_job(disk, job_id, with_files=False, created=NOW - created)
        hook_files(disk, job_id)
    redo(disk, "b", "a")
    redo(disk, "c", "b")
    redo(disk, "lone", "gone")                                                      # its parent is not in the database
    run(disk)
    assert [disk.store.get_job(j)["reused_stages"] for j in ("b", "c", "lone")] == [5, 5, 0]
    assert len(work_files(disk, "lone")) == 5 and len(work_files(disk, "a")) == 5


def test_a_parent_whose_files_are_gone_leaves_the_childs_copies_alone(disk):
    add_job(disk, "p", with_files=False, created=NOW - 5)
    add_job(disk, "c", with_files=False, created=NOW - 4)
    hook_files(disk, "c")
    redo(disk, "c", "p")
    result = run(disk)
    assert disk.store.get_job("c")["reused_stages"] == 0 and len(work_files(disk, "c")) == 5 and not result.due


def test_leftover_finished_ads_published_hooks_and_json_files_go_only_when_the_same_file_stays(disk):
    add_job(disk, "x")                                                              # ad + hook published, sidecars, work files
    hook_files(disk, "x")
    ad = Path(disk.store.get_job("x")["ad_path"])
    hook = Path(disk.store.get_job("x")["hook_path"])
    ad.write_bytes(b"final ad")
    put(disk, "x", "ad.mp4", b"final ad")                                           # the merge output that was copied
    hook.write_bytes(b"render_hook-data")                                           # same as the work hook video
    add_job(disk, "y")                                                              # its published hook has no work copy
    hy = Path(disk.store.get_job("y")["hook_path"])
    hy.write_bytes(b"only copy")
    hook_files(disk, "y", render_hook=b"something else")
    run(disk)
    assert ad.exists() and not disk.path("work_dir", "x", "ad.mp4").exists()
    assert not hook.exists() and disk.store.get_job("x")["hook_path"] is None
    assert hy.exists() and disk.store.get_job("y")["hook_path"] == str(hy)           # never the only copy
    assert not list(disk.path("ads_dir").rglob("*.json")) and not list(disk.path("hooks_dir").rglob("*.json"))


def test_an_image_ad_redo_that_kept_the_image_points_to_the_same_file(disk):
    add_job(disk, "old", ad_format="image", with_files=False, created=NOW - 5)
    add_job(disk, "new", ad_format="image", with_files=False, created=NOW - 4)
    old, new = (Path(disk.store.get_job(j)["ad_path"]) for j in ("old", "new"))
    old.write_bytes(b"image")
    new.write_bytes(b"image")
    redo(disk, "new", "old")
    run(disk)
    assert disk.store.get_job("new")["ad_path"] == str(old) and old.exists() and not new.exists()


def test_a_dry_run_changes_nothing_and_an_apply_does_not_touch_the_clock_of_the_cleanup(disk):
    add_job(disk, "p", with_files=False, created=NOW - 5)
    add_job(disk, "c", with_files=False, created=NOW - 4, updated=NOW - 3)
    hook_files(disk, "p")
    hook_files(disk, "c")
    redo(disk, "c", "p")
    before = disk.store.get_job("c")
    dry = run(disk, apply=False)
    assert dry.linked == ["c"] and len(dry.due) == 5 and not dry.deleted
    assert disk.store.get_job("c") == before and len(work_files(disk, "c")) == 5
    run(disk)
    after = disk.store.get_job("c")
    assert after["updated"] == before["updated"] and after["reused_stages"] == 5   # `updated` still counts the cleanup's days


def test_ads_still_being_made_are_left_alone_and_a_second_run_finds_nothing(disk, capsys):
    add_job(disk, "p", with_files=False, created=NOW - 5)
    add_job(disk, "c", with_files=False, created=NOW - 4, status="running")
    hook_files(disk, "p")
    hook_files(disk, "c")
    redo(disk, "c", "p")
    assert not run(disk).due
    disk.store.update_job("c", status="done")
    assert run(disk).linked == ["c"]
    assert not run(disk).due and run_job(disk.s, disk.store, apply=True) == 0
    assert "nothing is stored twice" in capsys.readouterr().out
