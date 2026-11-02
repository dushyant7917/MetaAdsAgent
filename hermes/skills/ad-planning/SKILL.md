---
name: ad-planning
description: How many new ads to create today, from the monthly budget. Use when the owner asks how many ads to make, what the budget allows, or how the month's spend stands.
---

# Planning how many new ads to make

Read-only: `plan_new_ads` creates and changes nothing. Call it with no arguments whenever the owner asks how many
new ads to create (today, this week, "what does the budget allow"), or how the monthly budget stands.

If it returns `error`, pass the reason on in plain words (for example the monthly budget is not set in the settings).

Answer from its numbers only, never from your own estimate:
1. The headline: `new_ads_total` new ads, to be uploaded for the launch on `launch_day` (new ads start spending at
   the next 6:00 IST, so today's spending is not theirs).
2. Why, in two or three lines: `budget_left_inr` of `monthly_budget_inr`, `committed_by_running_adsets_inr` that
   the running ad sets will still spend, `reserve_inr` held back, over `launch_days_left` launch days, at about
   `expected_cost_per_new_ad_inr` per new ad. If `notes` mention a cold start or a cap, say so.
3. Per language in `campaigns` with `new_ads` above 0, two kinds:
   - `proven_hook_ads`: a hook that already wins, put on a body it has not had. Name the hook (`hook_text`) and the
     `bodies`. These are redos of the ad `hook_job_id` with a new body.
   - `new_hook_ads`: that many brand-new hooks on the default body. You write nothing for them; the owner supplies
     the hook texts.
4. If `notes` say a hook was retired, mention it: its recent new bodies kept failing, so it is no longer reused.
5. If asked, `at_risk_adsets` (about to be paused, with days left) and `scaling_winners` (doing well, will get
   budget raises).

Do not create anything because of this answer. If the owner then says to go ahead:
- `new_hook_ads`: ask for the hook texts, then follow the `ad-creation` skill.
- `proven_hook_ads`: follow the `ad-redo` skill with `new_body` for each; it plans first and waits for approval. The new
  ads then wait for the owner's decision like any other (`ad-review`).
