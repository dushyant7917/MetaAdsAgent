---
name: ad-daily-plan
description: Daily ad plan ({{schedules.daily_plan_ist}} IST cron) and the owner's reply: approve, change or reject it. Use for the scheduled planning run and whenever the owner answers about a plan.
---

# The daily ad plan

Every day at {{schedules.daily_plan_ist}} IST a scheduled run plans the next launch's ads and sends the owner the plan to approve. Nothing is made
until they approve. After approval the ads are made, each waits for the owner's decision (`ad-review`), and approved
ads go to Meta (`meta-upload`). Two parts: the scheduled run, and the owner's reply.

## Part 1: the scheduled run ("Run the daily ad plan")
You are alone here: nobody answers questions. Your final reply is delivered to the owner on Telegram.
Never call `create_ads`, `approve_ad_plan` or `upload_ads` in this run.

1. `plan_new_ads`. If it returns `error`, make your final reply that reason in plain words and stop.
   If `new_ads_total` is 0, call `propose_ad_plan(new_hooks=[])` and make its `message` your final reply.
2. `get_hook_brief` (the product, the style rules, the owner's sample winning hooks, each language's script, the hooks
   already made) and `get_hook_performance` (the hooks that won and the ones that kept failing, with numbers).
3. Write the new hooks. For each language in `campaigns`, write exactly `new_hook_video_ads` hooks with
   `ad_format: "video"` and `new_hook_image_ads` hooks with `ad_format: "image"`. A hook has two texts:
   - `spoken_text`: the dialogue (it is also the Meta ad text). `thumbnail_text`: the words on the image; leave it out
     when the image should show the spoken words. An image ad has no voice, so its thumbnail text must work alone.
   - Write in the language's own script, product words (news, video, reporter, app, phone...) in English letters, as
     in the samples. Follow the brief's rules. Learn from the evidence: take the angle and style of the winners, avoid the
     patterns of the failures, never repeat a hook already made, and give every hook a different angle.
   - For every language except Hindi add `spoken_hindi`, and `thumbnail_hindi` when the thumbnail text differs from
     the spoken text: a faithful Hindi translation in Devanagari, so the owner can read what the hook says. It is for
     the message only and never goes into an ad.
   - Winning hooks (`proven_hook_ads`) are not rewritten. For those of a language other than Hindi pass their Hindi
     translation in `proven_glosses: [{hook_job_id, spoken_hindi, thumbnail_hindi?}]`.
4. `propose_ad_plan(new_hooks=[...], proven_glosses=[...])`. If it returns `errors`, fix exactly those hooks and call it
   again (up to {{ad_planning.proposal_retries}} times); `expected_new_hooks` shows the counts it needs. If it still fails, your final reply says what
   is wrong.
5. Keep one short note of what is working in your memory (the `memory` tool): format win rates (`format_stats`) and
   the best and worst hook styles per language, in at most {{ad_planning.memory_note_lines}} lines starting with "Ad performance notes (date):".
   Replace the earlier note of that name, never add a second one. State only numbers and hooks the tools returned (a
   winner is a hook in `winners`; do not guess). It is only a snapshot for chats; the tools are always the truth. If the memory tool refuses, skip this step.
6. Your final reply is the `message` returned by `propose_ad_plan`, exactly as it is, nothing added.

## Part 2: the owner replies to a plan
Always call `get_ad_plan` first (the plan is stored; this works in any chat session, also after `/new`). If `plan_id` is
None tell the owner no plan is waiting and what happened to the last one (`last_plan.status`).

| The owner says | You do |
|---|---|
| approve / ok / yes, go ahead | `approve_ad_plan(plan_id)`. Reply with one short line ("Making 12 ads. I'll send each one for your decision."). If `errors` came back, list them. The ads then follow `ad-review`. |
| change #3 to ..., drop #5, replace the hook of ... | `revise_ad_plan(plan_id, replace=[{n, spoken_text?, thumbnail_text?, spoken_hindi?, thumbnail_hindi?}], remove=[n])`, then show the returned `message`. The plan still waits for approval. A changed text for a language other than Hindi needs its new Hindi translation. |
| reject / skip today | `reject_ad_plan(plan_id)`, one short line. Nothing is made. |

Never guess: if the reply is vague ("hmm", "not sure", "looks off"), or mixes approval with a change, ask one short
question. Silence is not approval. Adding more ads than the plan is not possible (it would change the budget); say so.
If the tool says the plan is stale or superseded, tell the owner to wait for the next plan.
