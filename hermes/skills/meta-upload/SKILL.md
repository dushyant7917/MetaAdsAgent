---
name: meta-upload
description: Upload finished ads to Meta (ad sets and ads). Use when the owner asks to upload, publish or put ads on Meta, or asks if an ad is already there.
---

# Uploading ads to Meta

Only for ads the owner approved in the `ad-review` loop, or when the owner asks directly ("upload these", "put the
Tamil one on Meta"). It creates ACTIVE ad sets that start spending, so never do it on your own after creating ads.
`upload_ads` marks the ads approved, so they stop waiting for review.

1. Find the ads: the batch the owner replied to or named (`batch_id`), or specific ads via `list_ads` (`job_ids`).
   Every ad's caption ends with `id <job_id>`: when the owner replies to an ad, use that id as the `job_id`.
   `list_ads` shows `meta_status` / `meta_ad_id`: ads already on Meta are skipped automatically.
2. Call `upload_ads(batch_id=...)` or `upload_ads(job_ids=[...])`. Ad sets start at the next 6:00 IST by default; pass `start_time` (e.g. `2026-11-03T09:00`, IST) only when
   the owner names a start time. Add `dry_run=true` only if the owner wants to
   see what would be sent first.
3. If it returns `started: true`: one short line ("Uploading 3 ads to Meta"). The result arrives in the chat by
   itself (ad set and ad ids per ad, or the reason one failed). Use `get_upload_status(upload_id)` only when asked.
4. If `started: false`, pass on the `error` (an unfinished ad, a language with no campaign, a missing .env value).
5. If an upload failed, tell the owner the reason and offer to try again: calling `upload_ads` again for the same
   ads continues where it stopped and creates nothing twice.

## Automatic changes after upload (not done by you)
Two timers outside Hermes change ad sets on their own by fixed rules, and report on Telegram when they act. Do not
repeat or undo their work; if the owner asks, only explain it:
- Auto-pause, every 2 hours: pauses an ACTIVE ad set with no results once it has spent too much, or whose cost per
  result is above the limit for its number of results.
- Budget increase, daily at 5:00 IST: raises an ACTIVE ad set's daily budget by 20% when 70 hours have passed since
  its last increase, or, if never raised, once it has 50 results. It never goes above the configured maximum.
