---
name: ad-review
description: Approve, drop or redo ads before Meta upload. Use right after create_ads and whenever the owner answers about finished ads or asks what is waiting.
---

# Reviewing finished ads, then uploading

New ads are never uploaded on their own. Each one waits for the owner's decision, and the loop runs until no ad is
waiting or the owner says stop.

## After `create_ads`
Reply with one short line ("Making 3 ads. I'll ask you to approve them when they are ready") and stop. The ads
and a summary ending in "Waiting for your decision" arrive in the chat by themselves. Only if the owner said to just make
the ads and not upload them, pass `review=false` to `create_ads` and skip this skill.

## When the owner answers
`list_ads(awaiting_review=true)` is the source of truth for which ads are waiting (also after `/new`). Match each ad the
owner talks about by reply-to, id, or language and position. Every waiting ad must end up as exactly one of:

| The owner says | You do |
|---|---|
| approve / ok / upload (one, some, "approve all", "rest are fine") | one `upload_ads(job_ids=[...])` for all approved ads together |
| drop / delete / discard it | `drop_ads(job_ids=[...])` |
| what is wrong or what to change ("image is bad", "voice cut off", "different body") | pick `changes` with the table in `ad-redo`, then `plan_redo` and straight away `approve_redo`: the feedback is the go-ahead, do not ask "go ahead?". Reply with one line saying what is being redone (it costs a Veo clip when the image or video changes) |

The redone ad arrives in the chat with its own "Waiting for your decision" and goes through this loop again.

## Never guess: ask
Do not act on an ad, and ask about it, when:
- the owner rejects without saying drop or what to change ("reject 2", "not good", "redo 3", "I don't like it");
- the feedback does not tell which `changes` value fixes it;
- the reference fits more than one ad ("the Hindi one" when two are waiting);
- an ad waiting for a decision was not mentioned (silence is not approval);
- the reply mixes answers for the same ad.

Ask once, in one message, naming each unclear ad (language, id, first words of its hook) and giving the three choices:
approve, drop, or what to change. It may be one ad, several or all of them. Act at once on the ads that are clear; do not
hold approved ads back while you ask about the others.

## Stop and resume
"Stop" means stop: nothing more is uploaded or redone, and the ads stay waiting. When the owner says "continue" or
asks what is waiting, call `list_ads(awaiting_review=true)` and ask for decisions as above.

## Upload results
`upload_ads` follows the `meta-upload` skill: one short line, the result arrives in the chat. If an upload fails, tell
the owner the reason and offer to try again (calling `upload_ads` again continues where it stopped).
The loop is finished when no ad is waiting: every ad was uploaded or dropped.
