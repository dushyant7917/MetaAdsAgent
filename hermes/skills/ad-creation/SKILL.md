---
name: ad-creation
description: Make ad videos from a language and hook text (reporter hook + poster + body). Use when the owner asks to create ads, hooks or creatives, in one or many languages.
---

# Ad creation

Making new ads is fully automatic: no approval needed. Redoing a finished ad is the `ad-redo` skill and putting ads
on Meta is the `meta-upload` skill.

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
