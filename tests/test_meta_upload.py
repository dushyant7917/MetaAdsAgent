import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from metaads.config import Settings, load_settings
from metaads.jobs import JobStore
from metaads.meta import (GraphClient, ad_name, ad_op, build_targeting, default_start_time, parse_start_time, resolve_region_keys)
from metaads.pipeline import ValidationError
from metaads.uploader import MetaUploader

BASE = load_settings()
ENV = {"META_ACCESS_TOKEN": "tok", "META_AD_ACCOUNT_ID": "111", "META_PAGE_ID": "p1", "META_APP_ID": "a1",
       "META_STORE_URL": "https://play.google.com/x", "META_CAMPAIGN_ID_HINDI": "c-hi", "META_CAMPAIGN_ID_TAMIL": "c-ta"}
IST = timezone(timedelta(hours=5, minutes=30))


class FakeMeta:
    """Stands in for Graph: records every HTTP call and answers like Meta would."""

    def __init__(self, fail_adset_for: str | None = None, video_states=("ready",)):
        self.calls: list[tuple[str, str]] = []
        self.batches: list[list[dict]] = []
        self.n = 0
        self.video_states = list(video_states)
        self.fail_adset_for = fail_adset_for

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/", 2)[2] if req.url.path.count("/") >= 2 else ""
        self.calls.append((req.method, req.url.host + "/" + path))
        if path.endswith("advideos"):
            self.n += 1
            return httpx.Response(200, json={"id": f"vid{self.n}"})
        if path.endswith("adimages"):
            names = [part.split(b'name="')[1].split(b'"')[0].decode()
                     for part in req.read().split(b"Content-Disposition")[1:]]
            return httpx.Response(200, json={"images": {n: {"hash": f"h-{n}"} for n in names}})
        if req.method == "GET" and "ids" in req.url.params:
            state = self.video_states.pop(0) if len(self.video_states) > 1 else self.video_states[0]
            ids = req.url.params["ids"].split(",")
            return httpx.Response(200, json={i: {"id": i, "status": {"video_status": state}} for i in ids})
        if req.method == "GET" and path == "search":
            q = req.url.params["q"]
            return httpx.Response(200, json={"data": [{"key": f"key-{q}", "name": q, "country_code": "IN"}]})
        if req.method == "POST" and path == "":
            ops = json.loads(parse_qs(req.read().decode())["batch"][0])
            self.batches.append(ops)
            out, made = [], {}
            for op in ops:
                body = parse_qs(op["body"])
                if op["relative_url"].endswith("adsets"):
                    if self.fail_adset_for and self.fail_adset_for in body["name"][0]:
                        out.append({"code": 400, "body": json.dumps({"error": {"message": "bad targeting", "code": 100}})})
                        continue
                    made[op["name"]] = f"as-{op['name']}"
                    out.append({"code": 200, "body": json.dumps({"id": made[op["name"]]})})
                else:
                    ref = body["adset_id"][0]
                    if ref.startswith("{result="):
                        ref = ref[len("{result="):].split(":")[0]
                        if ref not in made:
                            out.append({"code": 400, "body": json.dumps({"error": {"message": "dependency failed", "code": 190}})})
                            continue
                    out.append({"code": 200, "body": json.dumps({"id": f"ad-{op['name']}"})})
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"error": {"message": f"unexpected {req.method} {path}", "code": 1}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    d = copy.deepcopy(BASE.to_dict())
    d["paths"]["db"] = str(tmp_path / "db.sqlite")
    d["paths"]["work_dir"] = str(tmp_path / "work")
    d["meta"]["regions_cache"] = str(tmp_path / "regions.json")
    s = Settings(d)
    store = JobStore(s.paths.db)
    batch_id, job_ids = store.create_batch(
        [{"language": "hi", "text": "News reporter ho to ye video apke liye hai! Aisi news video banani seekhni hai?",
          "body": "teal"}, {"language": "ta", "text": "Vanakkam", "body": "teal"}, {"language": "hi", "text": "Dusra", "body": None}])
    for jid in job_ids:
        (tmp_path / f"{jid}.mp4").write_bytes(b"video")
        img = tmp_path / "work" / jid / "image.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"png")
        store.update_job(jid, status="done", ad_path=str(tmp_path / f"{jid}.mp4"))
    fake = FakeMeta()
    sleeps: list[float] = []
    up = MetaUploader(s, store, client_factory=lambda tok: GraphClient(tok, "v25.0", transport=httpx.MockTransport(fake),
                                                                       sleep=lambda x: None, backoff=0),
                      sleep=sleeps.append, clock=lambda: datetime(2026, 11, 1, 8, 0, tzinfo=IST))
    return up, store, fake, job_ids, batch_id, sleeps


def _kinds(fake):
    return [c[1].split("/")[-1] for c in fake.calls]


def test_three_ads_in_n_plus_three_calls_and_one_time_region_lookups(env):
    up, store, fake, jobs, batch_id, _ = env
    res = up.submit(batch_id=batch_id, background=False)
    assert res["started"] and res["ads"] == 3
    kinds = _kinds(fake)
    assert kinds.count("advideos") == 3 and kinds.count("adimages") == 1 and len(fake.batches) == 1
    assert kinds.count("search") == 11                       # 10 Hindi states + Tamil Nadu, once ever
    assert len(kinds) - kinds.count("search") == 3 + 3       # N videos + 1 image call + 1 status read + 1 batch
    assert all(store.get_meta_upload(j)["status"] == "done" for j in jobs)
    assert [op["name"] for op in fake.batches[0]] == ["adset_0", "ad_0", "adset_1", "ad_1", "adset_2", "ad_2"]


def test_second_upload_reuses_cached_regions_and_never_repeats_an_ad(env):
    up, store, fake, jobs, batch_id, _ = env
    up.submit(job_ids=[jobs[0]], background=False)
    fake.calls.clear()
    res = up.submit(batch_id=batch_id, background=False)
    assert res["ads"] == 2 and [a["job_id"] for a in res["already_on_meta"]] == [jobs[0]]
    assert _kinds(fake).count("search") == 1                  # only Tamil Nadu is new; the 10 Hindi states are cached
    assert up.submit(batch_id=batch_id, background=False)["started"] is False         # all on Meta: zero calls


def test_ad_payload_has_inline_creative_dependency_and_state_targeting(env):
    up, store, fake, jobs, batch_id, _ = env
    up.submit(job_ids=[jobs[1]], background=False)                                      # Tamil
    adset, ad = fake.batches[0]
    a = parse_qs(adset["body"])
    assert a["campaign_id"] == ["c-ta"] and a["status"] == ["ACTIVE"] and a["name"] == [f"Tamil | 2026-11-01 | {jobs[1]}"]
    assert json.loads(a["targeting"][0])["geo_locations"] == {"regions": [{"key": "key-Tamil Nadu"}]}
    assert ad["body"].endswith("&adset_id={result=adset_0:$.id}")
    b = parse_qs(ad["body"])
    video = json.loads(b["creative"][0])["object_story_spec"]["video_data"]
    assert video["video_id"] == "vid1" and video["image_hash"] == f"h-{jobs[1]}.png" and video["message"] == "Vanakkam"
    assert b["name"] == [f"Tamil | teal | Vanakkam | {jobs[1]}"]


def test_failed_adset_fails_only_its_ad_and_a_rerun_resumes_without_reuploading(env):
    up, store, fake, jobs, batch_id, _ = env
    fake.fail_adset_for = jobs[1]
    up.submit(batch_id=batch_id, background=False)
    assert [store.get_meta_upload(j)["status"] for j in jobs] == ["done", "failed", "done"]
    assert "bad targeting" in store.get_meta_upload(jobs[1])["error"]
    assert store.get_meta_upload(jobs[1])["video_id"]                                     # kept for the retry
    fake.fail_adset_for = None
    fake.calls.clear()
    up.submit(batch_id=batch_id, background=False)
    assert "advideos" not in _kinds(fake) and "adimages" not in _kinds(fake)             # nothing uploaded twice
    assert store.get_meta_upload(jobs[1])["status"] == "done"


def test_waits_for_processing_videos_with_one_status_call_per_round(env):
    up, store, fake, jobs, batch_id, sleeps = env
    fake.video_states = ["processing", "processing", "ready"]
    up.submit(batch_id=batch_id, background=False)
    assert fake.calls.count(("GET", "graph.facebook.com/")) == 3 and sleeps == [5, 5]   # one status read per round


def test_video_that_never_gets_ready_fails_after_timeout(env):
    up, store, fake, jobs, batch_id, sleeps = env
    fake.video_states = ["processing"]
    up.submit(job_ids=[jobs[0]], background=False)
    row = store.get_meta_upload(jobs[0])
    assert row["status"] == "failed" and row["stage"] == "processing" and "not ready" in row["error"]
    assert not fake.batches


def test_validation_is_all_or_nothing(env, monkeypatch):
    up, store, fake, jobs, batch_id, _ = env
    with pytest.raises(ValidationError, match="unknown ad"):
        up.submit(job_ids=[jobs[0], "nope"])
    store.update_job(jobs[2], status="running")
    with pytest.raises(ValidationError, match="not finished"):
        up.submit(job_ids=[jobs[0], jobs[2]])
    monkeypatch.delenv("META_CAMPAIGN_ID_TAMIL")
    with pytest.raises(ValidationError, match="META_CAMPAIGN_ID_TAMIL"):
        up.submit(job_ids=[jobs[1]])
    with pytest.raises(ValidationError, match="either"):
        up.submit()
    assert not fake.calls


def test_dry_run_sends_nothing(env):
    up, store, fake, jobs, batch_id, _ = env
    res = up.submit(job_ids=[jobs[0]], dry_run=True)
    assert res["dry_run"] and res["ads"][0]["ops"][1]["name"] == "ad_0" and not fake.calls
    assert store.get_meta_upload(jobs[0]) is None


def test_asking_for_the_upload_approves_the_ads_but_a_dry_run_does_not(env):
    up, store, fake, jobs, batch_id, _ = env
    store.set_review(jobs, "pending")
    up.submit(job_ids=[jobs[0]], dry_run=True)
    assert store.get_job(jobs[0])["review"] == "pending"
    up.submit(job_ids=[jobs[0]], background=False)
    assert [store.get_job(j)["review"] for j in jobs] == ["approved", "pending", "pending"]


def test_a_refused_upload_approves_nothing(env):
    up, store, fake, jobs, batch_id, _ = env
    store.set_review(jobs, "pending")
    with pytest.raises(ValidationError):
        up.submit(job_ids=[jobs[0], "nope"])
    assert {store.get_job(j)["review"] for j in jobs} == {"pending"}


def test_rate_limit_is_waited_out_and_retried():
    waits, n = [], {"n": 0}

    def handler(req):
        n["n"] += 1
        if n["n"] == 1:
            return httpx.Response(400, json={"error": {"message": "slow down", "code": 17}})
        return httpx.Response(200, json={"id": "v1"})
    c = GraphClient("t", "v25.0", transport=httpx.MockTransport(handler), sleep=waits.append, backoff=30)
    assert c.get("x") == {"id": "v1"} and waits == [30]


def test_other_graph_errors_are_not_retried():
    c = GraphClient("t", "v25.0", transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"message": "bad", "code": 100, "error_user_msg": "Nope"}})))
    with pytest.raises(Exception, match="Nope"):
        c.get("x")


def test_batches_split_at_50_ops_without_separating_an_ad_from_its_ad_set():
    seen = []

    def handler(req):
        ops = json.loads(parse_qs(req.read().decode())["batch"][0])
        seen.append([o["name"] for o in ops])
        return httpx.Response(200, json=[{"code": 200, "body": json.dumps({"id": "1"})} for _ in ops])
    c = GraphClient("t", "v25.0", transport=httpx.MockTransport(handler))
    groups = [[{"name": f"adset_{i}", "body": ""}, ad_op(f"ad_{i}", "1", "n", {}, None, f"adset_{i}")] for i in range(26)]
    assert len(c.batch(groups)) == 52 and [len(x) for x in seen] == [50, 2]


def test_region_cache(tmp_path):
    calls = []
    c = GraphClient("t", "v25.0", transport=httpx.MockTransport(
        lambda r: calls.append(1) or httpx.Response(200, json={"data": [{"key": "7", "name": "Goa", "country_code": "IN"}]})))
    path = tmp_path / "r.json"
    assert resolve_region_keys(c, path, "IN", ["Goa"]) == ["7"] and resolve_region_keys(c, path, "IN", ["Goa"]) == ["7"]
    assert len(calls) == 1 and resolve_region_keys(None, path, "IN", ["Goa", "Other"]) == ["7", None]


@pytest.mark.parametrize("hour,expected", [(0, "2026-11-01T06:00:00+05:30"), (5, "2026-11-01T06:00:00+05:30"),
                                          (6, "2026-11-02T06:00:00+05:30"), (10, "2026-11-02T06:00:00+05:30"),
                                          (23, "2026-11-02T06:00:00+05:30")])
def test_default_start_time_is_the_next_6am_ist(hour, expected):
    assert default_start_time(datetime(2026, 11, 1, hour, 30, tzinfo=IST)) == expected
    assert default_start_time(datetime(2026, 11, 1, hour, 30, tzinfo=IST).astimezone(timezone.utc)) == expected


def test_owner_start_time_overrides_and_naive_means_ist(env):
    up, store, fake, jobs, batch_id, _ = env
    assert parse_start_time("2026-11-03T09:00") == "2026-11-03T09:00:00+05:30"
    assert parse_start_time("2026-11-03T09:00:00+00:00") == "2026-11-03T09:00:00+00:00"
    up.submit(job_ids=[jobs[1]], background=False, start_time="2026-11-03T09:00")
    assert parse_qs(fake.batches[0][0]["body"])["start_time"] == ["2026-11-03T09:00:00+05:30"]
    with pytest.raises(ValidationError, match="ISO 8601"):
        up.submit(job_ids=[jobs[0]], start_time="tomorrow morning")


def test_names_and_targeting():
    assert ad_name("Hindi", "teal", "News reporter ho to ye video apke liye hai! Aisi news", "b1-1") == \
        "Hindi | teal | News reporter ho to ye video apke liye… | b1-1"
    assert ad_name("Hindi", None, "a | b\nc", "j") == "Hindi | - | a b c | j"
    m = BASE.meta
    assert build_targeting(m, [])["geo_locations"] == {"countries": ["IN"]}
    assert build_targeting(m, [])["user_os"] == ["Android_ver_10.0_and_above"]
