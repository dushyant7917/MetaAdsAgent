"""Upload finished ads to Meta: one ad set + one ad per job, in as few Graph API calls as possible.

For N ads: N video uploads, 1 image call, 1+ status read (all videos at once), 1 batch call (every ad set and ad,
creative inline), plus a region lookup only for a state never seen before. Everything Meta returns is saved per job
in `meta_uploads`, so a rerun resumes where it stopped and never creates anything twice.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import media
from .adaccount import campaign_env_var
from .config import Settings, _resolve_path
from .jobs import JobStore
from .locks import CPU_LOCK
from .meta import (GraphClient, GraphError, ad_name, ad_op, ad_set_name, adset_op, build_adset_fields,
                   build_creative, build_targeting, default_start_time, parse_start_time, resolve_region_keys)
from .notify import Notifier, NullNotifier
from .pipeline import ValidationError

log = logging.getLogger("metaads.uploader")

REQUIRED_ENV = ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID", "META_APP_ID", "META_STORE_URL")


@dataclass
class MetaEnv:
    token: str
    ad_account_id: str
    page_id: str
    app_id: str
    store_url: str
    ig_user_id: str
    campaigns: dict[str, str]          # language code -> campaign id


class MetaUploader:
    def __init__(self, settings: Settings, store: JobStore, notifier: Notifier | None = None,
                 client_factory: Callable[[str], GraphClient] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.s = settings
        self.store = store
        self.notifier = notifier or NullNotifier()
        self.client_factory = client_factory or (lambda token: GraphClient(token, settings.meta.api_version))
        self.sleep = sleep
        self.clock = clock
        self._active: set[str] = set()
        self._lock = threading.Lock()

    # ---------- validation ----------
    def load_env(self, languages: set[str]) -> MetaEnv:
        missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
        campaigns = {}
        for code in sorted(languages):
            var = campaign_env_var(self.s.languages[code])
            if os.environ.get(var, "").strip():
                campaigns[code] = os.environ[var].strip()
            else:
                missing.append(var)
        if missing:
            raise ValidationError("Missing in .env: " + ", ".join(missing))
        g = lambda k: os.environ[k].strip()
        return MetaEnv(g("META_ACCESS_TOKEN"), g("META_AD_ACCOUNT_ID").removeprefix("act_"), g("META_PAGE_ID"),
                       g("META_APP_ID"), g("META_STORE_URL"), os.environ.get("META_IG_USER_ID", "").strip(), campaigns)

    def select(self, job_ids: list[str] | None, batch_id: str | None) -> tuple[list[dict], list[dict], list[str]]:
        """-> (jobs to upload, already on Meta, skipped because unfinished). Raises ValidationError, all or nothing."""
        if bool(job_ids) == bool(batch_id):
            raise ValidationError("Give either job_ids or batch_id")
        skipped: list[str] = []
        if batch_id:
            try:
                jobs = self.store.batch_jobs(batch_id)
            except KeyError:
                raise ValidationError(f"Unknown batch {batch_id}") from None
            skipped = [j["id"] for j in jobs if j["status"] != "done"]
            jobs = [j for j in jobs if j["status"] == "done"]
        else:
            jobs, errors = [], []
            for jid in dict.fromkeys(job_ids):
                try:
                    job = self.store.get_job(jid)
                except KeyError:
                    errors.append(f"{jid}: unknown ad")
                    continue
                if job["status"] != "done":
                    errors.append(f"{jid}: not finished (status {job['status']})")
                else:
                    jobs.append(job)
            if errors:
                raise ValidationError("; ".join(errors))
        errors = [f"{j['id']}: video file missing ({j['ad_path']})" for j in jobs
                  if not j["ad_path"] or not Path(j["ad_path"]).is_file()]
        if errors:
            raise ValidationError("; ".join(errors))
        done, todo = [], []
        for j in jobs:
            row = self.store.get_meta_upload(j["id"])
            (done if row and row["ad_id"] else todo).append({**j, "meta": row})
        if not jobs:
            raise ValidationError("No finished ads to upload")
        if len(todo) > self.s.limits.max_meta_upload:
            raise ValidationError(f"At most {self.s.limits.max_meta_upload} ads per upload, got {len(todo)}")
        busy = [j["id"] for j in todo if j["id"] in self._active]
        if busy:
            raise ValidationError(f"Already being uploaded: {', '.join(busy)}")
        return todo, done, skipped

    # ---------- entry point ----------
    def submit(self, job_ids: list[str] | None = None, batch_id: str | None = None, chat_id: str | None = None,
               dry_run: bool = False, background: bool = True, start_time: str | None = None) -> dict[str, Any]:
        if start_time:
            try:
                start_time = parse_start_time(start_time)
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
        todo, done, skipped = self.select(job_ids, batch_id)
        already = [{"job_id": j["id"], "ad_id": j["meta"]["ad_id"]} for j in done]
        extra = {"already_on_meta": already, "skipped_unfinished": skipped}
        if not todo:
            return {"started": False, "error": "Everything requested is already on Meta", **extra}
        env = self.load_env({j["language"] for j in todo})
        if dry_run:
            return {"started": False, "dry_run": True, "ads": self._dry_run(todo, env, start_time), **extra}
        upload_id = "u" + uuid.uuid4().hex[:8]
        with self._lock:
            self._active.update(j["id"] for j in todo)
        for j in todo:
            self.store.upsert_meta_upload(j["id"], upload_id=upload_id, status="queued", stage="queued", error=None)
        args = (upload_id, todo, env, chat_id, start_time)
        if background:
            threading.Thread(target=self._run, args=args, daemon=True, name=f"metaads-meta-{upload_id}").start()
        else:
            self._run(*args)
        return {"started": True, "upload_id": upload_id, "ads": len(todo), **extra}

    def status(self, upload_id: str) -> dict[str, Any]:
        rows = self.store.meta_uploads_for(upload_id)
        if not rows:
            raise KeyError(upload_id)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"upload_id": upload_id, "counts": counts,
                "ads": [{k: r[k] for k in ("job_id", "status", "stage", "error", "video_id", "adset_id", "ad_id")}
                        for r in rows]}

    # ---------- payloads ----------
    def _group(self, job: dict, i: int, env: MetaEnv, day: str, start: str, region_keys: list[str],
               video_id: str, image_hash: str, adset_id: str | None) -> list[dict[str, Any]]:
        cfg, acct = self.s.meta, env.ad_account_id
        lang = self.s.languages[job["language"]]
        creative = build_creative(cfg, ad_name(lang, job["body"], job["text"], job["id"]), env.page_id,
                                  env.ig_user_id, video_id, image_hash, job["text"], env.store_url)
        group = []
        if not adset_id:
            fields = build_adset_fields(cfg, ad_set_name(lang, day, job["id"]), env.campaigns[job["language"]], start,
                                        build_targeting(cfg, region_keys), env.app_id, env.store_url)
            group.append(adset_op(f"adset_{i}", acct, fields))
        group.append(ad_op(f"ad_{i}", acct, creative["name"], creative, adset_id, f"adset_{i}"))
        return group

    def _dry_run(self, jobs: list[dict], env: MetaEnv, start_time: str | None) -> list[dict[str, Any]]:
        now = self.clock()
        day, start = now.astimezone().strftime("%Y-%m-%d"), start_time or default_start_time(now)
        out = []
        for i, job in enumerate(jobs):
            states = self.s.meta.language_states.get(job["language"]) or []
            keys = resolve_region_keys(None, _resolve_path(self.s.meta.regions_cache), self.s.meta.country, states)
            keys = [k or f"<region key of {st}>" for k, st in zip(keys, states)]
            ops = self._group(job, i, env, day, start, keys, "<video_id>", "<image_hash>", None)
            out.append({"job_id": job["id"], "video": job["ad_path"], "ops": ops})
        return out

    # ---------- the upload ----------
    def _run(self, upload_id: str, jobs: list[dict], env: MetaEnv, chat_id: str | None,
             start_time: str | None = None) -> None:
        failed: dict[str, str] = {}
        start = start_time or default_start_time(self.clock())
        try:
            self._upload(upload_id, jobs, env, failed, start)
        except Exception as exc:                                          # a bug must not leave jobs "running"
            log.exception("meta upload %s crashed", upload_id)
            for j in jobs:
                if j["id"] not in failed and not (self.store.get_meta_upload(j["id"]) or {}).get("ad_id"):
                    self._fail(failed, j["id"], "crashed", exc)
        finally:
            with self._lock:
                self._active.difference_update(j["id"] for j in jobs)
        try:
            self.notifier.send_report(chat_id, self._report(jobs, start))
        except Exception:
            log.exception("notification failed")

    def _fail(self, failed: dict[str, str], job_id: str, stage: str, exc: Exception | str) -> None:
        failed[job_id] = f"{stage}: {exc}"
        log.warning("meta upload %s failed at %s: %s", job_id, stage, exc)
        self.store.upsert_meta_upload(job_id, status="failed", stage=stage, error=str(exc))

    def _upload(self, upload_id: str, jobs: list[dict], env: MetaEnv, failed: dict[str, str], start: str) -> None:
        cfg = self.s.meta
        client = self.client_factory(env.token)
        acct = env.ad_account_id
        state = {j["id"]: dict(self.store.get_meta_upload(j["id"]) or {}) for j in jobs}
        save = lambda jid, **f: (state[jid].update(f), self.store.upsert_meta_upload(jid, **f))
        live = lambda: [j for j in jobs if j["id"] not in failed]
        for j in jobs:
            self.store.upsert_meta_upload(j["id"], status="running")

        # 1. region keys: cached forever, so normally no call
        regions: dict[str, list[str]] = {}
        for lang in {j["language"] for j in jobs}:
            try:
                regions[lang] = resolve_region_keys(client, _resolve_path(cfg.regions_cache), cfg.country,
                                                    cfg.language_states.get(lang) or [])
            except GraphError as exc:
                for j in jobs:
                    if j["language"] == lang:
                        self._fail(failed, j["id"], "targeting", exc)

        # 2. videos: one call each, in parallel
        def upload_video(job: dict) -> None:
            try:
                save(job["id"], stage="video")
                save(job["id"], video_id=client.upload_video(acct, job["ad_path"], Path(job["ad_path"]).name),
                     stage="processing")
            except (GraphError, OSError) as exc:
                self._fail(failed, job["id"], "video", exc)
        need = [j for j in live() if not state[j["id"]].get("video_id")]
        if need:
            with ThreadPoolExecutor(max_workers=min(self.s.limits.api_concurrency, len(need))) as pool:
                list(pool.map(upload_video, need))

        # 3. thumbnails: the reporter image, ALL of them in one call
        need = [j for j in live() if not state[j["id"]].get("image_hash")]
        files: dict[str, Path] = {}
        for j in need:
            try:
                files[f"{j['id']}.png"] = self._thumbnail(j)
            except Exception as exc:
                self._fail(failed, j["id"], "thumbnail", exc)
        if files:
            try:
                hashes = client.upload_images(acct, files)
            except GraphError as exc:
                hashes = {}
                for name in files:
                    self._fail(failed, name.removesuffix(".png"), "thumbnail", exc)
            for name in files:
                jid = name.removesuffix(".png")
                if jid in failed:
                    continue
                if name in hashes:
                    save(jid, image_hash=hashes[name])
                else:
                    self._fail(failed, jid, "thumbnail", "Meta returned no image hash")

        # 4. wait until Meta has processed every video (one status call covers all of them)
        pending = {state[j["id"]]["video_id"]: j["id"] for j in live()}
        waited = 0.0
        while pending:
            try:
                statuses = client.video_statuses(list(pending))
            except GraphError as exc:
                for jid in pending.values():
                    self._fail(failed, jid, "processing", exc)
                break
            for vid, st in statuses.items():
                if st == "ready":
                    pending.pop(vid)
                elif st == "error":
                    self._fail(failed, pending.pop(vid), "processing", f"Meta could not process video {vid}")
            if pending and waited >= cfg.video_ready_timeout:
                for jid in pending.values():
                    self._fail(failed, jid, "processing", f"video not ready after {cfg.video_ready_timeout}s")
                break
            if pending:
                self.sleep(cfg.video_poll_seconds)
                waited += cfg.video_poll_seconds

        # 5. every ad set + ad (creative inline) in one batch
        ready = live()
        if not ready:
            return
        now = self.clock()
        day = now.astimezone().strftime("%Y-%m-%d")
        groups = []
        for i, j in enumerate(ready):
            st = state[j["id"]]
            groups.append(self._group(j, i, env, day, start, regions[j["language"]], st["video_id"],
                                      st["image_hash"], st.get("adset_id")))
            save(j["id"], stage="publish")
        try:
            results = client.batch(groups)
        except GraphError as exc:
            for j in ready:
                self._fail(failed, j["id"], "publish", exc)
            return
        for i, j in enumerate(ready):
            adset, ad = results.get(f"adset_{i}"), results[f"ad_{i}"]
            if adset and adset.ok:
                save(j["id"], adset_id=adset.id)
            if adset and not adset.ok:
                self._fail(failed, j["id"], "ad set", adset.error)
            elif not ad.ok:
                made = f" (ad set {state[j['id']]['adset_id']} was created)" if state[j["id"]].get("adset_id") else ""
                self._fail(failed, j["id"], "ad", f"{ad.error}{made}")
            else:
                save(j["id"], ad_id=ad.id, status="done", stage="done", error=None)

    def _thumbnail(self, job: dict) -> Path:
        work = Path(self.s.paths.work_dir) / job["id"]
        image = work / "image.png"                       # what Veo animated: the ad's own first frame, already 9:16
        if image.is_file():
            return image
        work.mkdir(parents=True, exist_ok=True)
        with CPU_LOCK:
            return media.extract_frame(job["ad_path"], 0.0, work / "meta_thumb.png", self.s)

    def _report(self, jobs: list[dict], start: str) -> str:
        rows = [self.store.get_meta_upload(j["id"]) or {} for j in jobs]
        ok = [r for r in rows if r.get("status") == "done"]
        lines = [f"{len(ok)} of {len(jobs)} ads uploaded to Meta."]
        for j, r in zip(jobs, rows):
            if r.get("status") == "done":
                lines.append(f"{j['id']} ({j['language']}): ad set {r['adset_id']}, ad {r['ad_id']}")
            else:
                lines.append(f"{j['id']} ({j['language']}) failed at {r.get('stage')}: {r.get('error')}")
        if ok:
            lines.append(f"Ads start at {start}.")
        if len(ok) < len(jobs):
            lines.append("Asking again for the failed ones continues where they stopped; nothing is created twice.")
        return "\n".join(lines)
