---
name: ad-creation
description: Make ads from a language and hook text (video or 4:5 image). Use when the owner asks to create ads, hooks or creatives, in one or many languages.
---

# Ad creation

Making new ads is fully automatic: no approval needed. But the finished ads are not uploaded until the owner approves
them: after `create_ads`, continue with the `ad-review` skill. Redoing a finished ad is the `ad-redo` skill and putting
ads on Meta is the `meta-upload` skill.

## What the tools do
`create_ads` runs the whole chain for each ad: a reporter image (with the thumbnail text), a talking-reporter video
(Veo) saying the spoken text, the poster demo shown over the right spoken words, then the body video joined with a
cross-fade. Files are saved under `Hooks/<language>` and `Ads/<language>` and each finished ad is sent to this chat
automatically. An ad with `ad_format: "image"` is just one 4:5 image with the thumbnail text (no video, cheaper), made
only when the owner asks for it (or the daily plan has one); it arrives as a photo marked "Image ad". A video ad whose hook
video fails (after a retry) fails: it is never replaced by an image ad.

## Reading the request
Languages are codes: hi (Hindi), bn (Bengali), gu (Gujarati), kn (Kannada), mr (Marathi), ta (Tamil),
te (Telugu). Accept language names too and convert them. Examples of what the owner may send:

- `hi: News reporter ho to ye video apke liye hai! Aisi news video banani seekhni hai?`
  -> one item `{language: "hi", spoken_text: "News reporter ho to ..."}` (the image shows the same words)
- The owner gives two texts ("thumbnail: ... / dialogue: ..." or "image text | spoken text")
  -> `{language, thumbnail_text: <image words>, spoken_text: <dialogue>}`.
- "image ad" / "only an image" -> add `ad_format: "image"` to the items it applies to (default is video).
- Several lines or a numbered list with a language each -> one item per line.
- "Make these in Tamil and Telugu: <text>" (same text, two languages) -> two items, same text.
- "use the teal body" / "peach body" -> add `body: "teal"` to the items it applies to.
  Default body is `navy-darkyellow-yellowbox`; call `list_languages` to see the options.
- "2 ads in hindi with this text" -> two identical items (each becomes its own ad).

If the language is missing, ask which one. If you cannot tell where one hook ends and the next begins,
ask. Otherwise just go.

## Steps
1. Build `items` (one per ad). Keep the texts exactly as written.
2. Call `create_ads(items=[...])`.
3. If it returns `started: true`: reply with one short line naming how many ads and which languages (and that you will
   ask for approval when they are ready, see `ad-review`).
4. If `started: false`: pass on the `error` and what to change.
5. Stop. The ads and a final "N of N ads created" summary arrive in the chat on their own.
   When the owner asks for progress, call `get_batch_status(batch_id)` and summarise it.
6. If a delivery to the chat did not arrive, you can attach the file yourself from `get_batch_status`
   using `MEDIA:<ad_path>`.

## Limits to mention only when they matter
- Gemini estimates how long each spoken text takes to speak (one call per text, run concurrently) and picks a clip of
  {{hook.durations}} seconds. Text that needs more than the longest of those still gets the longest clip, so the end of the speech
  may be cut off (the ad's caption warns when that happens); if the estimate call keeps
  failing the request is refused with "try again".
- At most {{limits.max_batch_items}} ads per message and a daily cap of {{limits.daily_video_cap}} videos; `create_ads` refuses a request over a limit and the error says so.
- Video generation is limited to {{limits.video_rpm}} requests per minute, so a big batch takes several minutes. If an ad
  fails (for example the video model's safety filter), say so with the reason and offer to try again.

## Building blocks (only for specific requests)
`generate_image` (`thumbnail_text`, 9:16, or 4:5 with `image_ad`), `generate_video` (`spoken_text`), `transcribe_words`,
`plan_poster_window`, `render_hook`, `merge_ad` exist for redoing one step, for example re-merging a hook with another body. Use them only when the owner
asks for that; otherwise use `create_ads`.
