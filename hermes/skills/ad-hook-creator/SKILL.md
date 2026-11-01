---
name: ad-hook-creator
description: Create Meta ad videos (reporter hook + poster demo + body, cross-faded) from a language and hook text. Use when the owner asks for ads, hooks or creatives, in one or many languages.
---

# Ad hook creator

## What the tools do
`create_ads` runs the whole chain for each ad: a reporter image, a talking-reporter video (Veo), the poster demo shown
over the right spoken words, then the body video joined with a cross-fade. Files are saved under
`Hooks/<language>` and `Ads/<language>` and each finished ad is sent to this chat automatically.

## Reading the request
Languages are codes: hi (Hindi), bn (Bengali), gu (Gujarati), kn (Kannada), mr (Marathi), ta (Tamil),
te (Telugu). Accept language names too and convert them. Examples of what the owner may send:

- `hi: News reporter ho to ye video apke liye hai! Aisi news video banani seekhni hai?`
  -> one item `{language: "hi", text: "News reporter ho to ..."}`
- Several lines or a numbered list with a language each -> one item per line.
- "Make these in Tamil and Telugu: <text>" (same text, two languages) -> two items, same text.
- "use the teal body" / "peach body" -> add `body: "teal"` to the items it applies to.
  Default body is `navy-darkyellow-yellowbox`; call `list_languages` to see the options.
- "2 ads in hindi with this text" -> two identical items (each becomes its own ad).

If the language is missing, ask which one. If you cannot tell where one hook ends and the next begins,
ask. Otherwise just go.

## Steps
1. Build `items` (one per ad). Keep the text exactly as written.
2. Call `create_ads(items=[...])`.
3. If it returns `started: true`: reply with one short line naming how many ads and which languages.
4. If `started: false`: pass on the `error` and what to change.
5. Stop. The ads and a final "N of N ads created" summary arrive in the chat on their own.
   When the owner asks for progress, call `get_batch_status(batch_id)` and summarise it.
6. If a delivery to the chat did not arrive, you can attach the file yourself from `get_batch_status`
   using `MEDIA:<ad_path>`.

## Limits to mention only when they matter
- Gemini estimates how long each text takes to speak (one call per text, run concurrently) and picks a 4, 6 or
  8 second clip. Text that needs more than 8 seconds still gets the 8 second clip, so the end of the speech
  may be cut off (the ad's caption warns when that happens); if the estimate call keeps
  failing the request is refused with "try again".
- At most 10 ads per message and a daily cap on videos.
- Video generation is limited to 4 requests per minute, so a big batch takes several minutes. If an ad
  fails (for example the video model's safety filter), say so with the reason and offer to try again.

## Building blocks (only for specific requests)
`generate_image`, `generate_video`, `transcribe_words`, `plan_poster_window`, `render_hook`, `merge_ad`
exist for redoing one step, for example re-merging a hook with another body. Use them only when the owner
asks for that; otherwise use `create_ads`.

## Redoing part of an ad (when the owner is not happy)
Redo only what has to change and reuse the rest. Every redo is **plan, show, wait for approval, then run**.

1. **Find the ad.** Use the batch/ad the owner replied to or named. If unclear, `list_ads` and, if still unclear,
   ask which one ("the Tamil one or the second Hindi one?"). Then `get_ad_details(job_id)`: it shows the text,
   the spoken words, whether the voice may be cut off, and when the poster appears.
2. **Decide what is wrong.**
   - If the owner says exactly what to redo or keep ("only the image", "keep the video, just move the poster"),
     do exactly that.
   - If they only say what is wrong, work out the smallest change from this table.
   - If you cannot tell what is wrong ("I don't like it"), ask ONE short question with the options:
     the reporter image, the voice or video, the poster timing, the body video, the text. Do not guess when it is unclear.

   | Owner says | `changes` | Why |
   |---|---|---|
   | image / reporter / thumbnail is bad | `["image"]` | A new image needs a new Veo clip, so everything after it is new too. The clip length is kept. |
   | voice / lip-sync / video / picture is bad, image is fine | `["video"]` | Same image, new Veo clip. |
   | voice is cut off, or the clip is too short | `["duration"]` with the next longer `duration` | If it is already 8 s, say the text is too long and offer to shorten it (`text`). |
   | poster appears at the wrong moment | `["poster_timing"]` | Reuses the image, clip and word timings. No Veo cost. Pass `poster_start`/`poster_end` for an exact time, or `poster_feedback` in the owner's words. |
   | wrong body video | `["body"]` with `new_body` | Only re-joins hook and body. |
   | hook text is wrong | `["text"]` with `new_text` | Everything is new. A different language is a new ad: use `create_ads`. |

   Whisper and the poster planner read the Veo clip, so they are redone whenever the clip is new, and skipped
   whenever the clip is kept. `plan_redo` works this out; you do not need to.
3. **Plan.** Call `plan_redo(...)`. It runs nothing and spends nothing.
4. **Show the plan** in plain words: what will be redone, what will be reused from the old ad, and the cost
   (Veo clip, image). Keep it short, then ask for approval. Example: "I'll redo: new image, new Veo clip,
   Whisper, poster timing, render, merge (1 image + 1 Veo call). I'll reuse: the clip length and the body video.
   Go ahead?"
5. **Wait for the owner's answer.** Only after a clear yes, call `approve_redo(plan_id)`. If they change the plan,
   call `plan_redo` again. If they say no, drop it. Never call `approve_redo` on your own.
6. After approval reply with one short line. The new ad arrives in the chat by itself; the old one is kept.
