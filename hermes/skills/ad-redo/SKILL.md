---
name: ad-redo
description: Redo part of a finished ad (image, voice, poster timing, body, thumbnail or spoken text) after feedback. Always plan, show, wait for approval, then run.
---

# Redoing part of an ad (when the owner is not happy)

Redo only what has to change and reuse the rest. Every standalone redo is **plan, show, wait for approval, then run**.
Exception: for an ad waiting in the `ad-review` loop, the owner's feedback already is the approval, so call `plan_redo`
and `approve_redo` at once (steps 1 to 3 below still apply; skip steps 4 and 5). The redone ad goes through the review again.

1. **Find the ad.** Use the batch/ad the owner replied to or named. If unclear, `list_ads` and, if still unclear,
   ask which one ("the Tamil one or the second Hindi one?"). Then `get_ad_details(job_id)`: it shows both texts
   (`thumbnail_text` on the image, `spoken_text` the dialogue), the format, the spoken words, whether the voice may be
   cut off, and when the poster appears.
2. **Decide what is wrong.**
   - If the owner says exactly what to redo or keep ("only the image", "keep the video, just move the poster"),
     do exactly that.
   - If they only say what is wrong, work out the smallest change from this table.
   - If you cannot tell what is wrong ("I don't like it"), ask ONE short question with the options:
     the reporter image, the voice or video, the poster timing, the body video, the words on the image, the dialogue.
     Do not guess when it is unclear.

   | Owner says | `changes` | Why |
   |---|---|---|
   | image / reporter / thumbnail is bad | `["image"]` | A new image needs a new Veo clip, so everything after it is new too. The clip length is kept. |
   | voice / lip-sync / video / picture is bad, image is fine | `["video"]` | Same image, new Veo clip. |
   | voice is cut off, or the clip is too short | `["duration"]` with the next longer `duration` | If it is already the longest clip ({{hook.durations}} s), say the text is too long and offer to shorten it (`text`). |
   | poster appears at the wrong moment | `["poster_timing"]` | Reuses the image, clip and word timings. No Veo cost. Pass `poster_start`/`poster_end` for an exact time, or `poster_feedback` in the owner's words. |
   | wrong body video | `["body"]` with `new_body` | Only re-joins hook and body. |
   | words on the image are wrong | `["thumbnail_text"]` with `new_thumbnail_text` | A new image, so a new Veo clip and everything after (the dialogue and clip length are kept). |
   | what she says is wrong (the dialogue) | `["spoken_text"]` with `new_spoken_text` | The image is kept; a new Veo clip, new timings. The words on the image stay as they were: ask whether they should change too (then add `thumbnail_text`). A different language is a new ad: use `create_ads`. |

   Whisper and the poster planner read the Veo clip, so they are redone whenever the clip is new, and skipped
   whenever the clip is kept. `plan_redo` works this out; you do not need to.

   Image ads (`ad_format: image` in `get_ad_details` / `list_ads`): an image ad the owner asked for can only redo
   `thumbnail_text` (new image), `image` (new image) or `spoken_text` (only the Meta ad text; the image is kept), and
   stays an image ad. Any other change (a body, the video) turns it into a video ad. An image ad that has a
   `fallback_reason` was made because the hook video failed: `["video"]` tries the Veo clip again from the stored
   reporter image, and the result is a video ad.
3. **Plan.** Call `plan_redo(...)`. It runs nothing and spends nothing.
4. **Show the plan** in plain words: what will be redone, what will be reused from the old ad, and the cost
   (Veo clip, image). Keep it short, then ask for approval. Example: "I'll redo: new image, new Veo clip,
   Whisper, poster timing, render, merge (1 image + 1 Veo call). I'll reuse: the clip length and the body video.
   Go ahead?"
5. **Wait for the owner's answer.** Only after a clear yes, call `approve_redo(plan_id)`. If they change the plan,
   call `plan_redo` again. If they say no, drop it. Never call `approve_redo` on your own.
6. After approval reply with one short line. The new ad arrives in the chat by itself; the old one is kept.
