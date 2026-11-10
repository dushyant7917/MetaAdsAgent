"""What the ads made so far have achieved, shared by the planner and the hook-performance tool: which ad sets won,
how well image ads do against video ads, and which hooks won or kept failing. Pure functions over `AdSet`s; reading
Meta and the jobs database is metaads.planning's job."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .jobs import FORMAT_IMAGE, FORMAT_VIDEO


@dataclass
class AdSet:
    id: str
    name: str
    language: str                   # language name, e.g. Hindi
    active: bool
    daily_budget: int | None        # paise; None when the budget belongs to the campaign
    spend: float                    # lifetime, Rs
    results: int                    # lifetime
    recent_spend: float | None      # Rs per day over the last full days it delivered; None without recent delivery
    recent_results: float           # per day, same days
    spend_today: float
    last_raise: float | None        # epoch of the last increase by metaads.budget
    job_id: str | None              # the job whose ad this is, when this tool made it
    hook: tuple[str, str, str] | None    # (language code, spoken text, thumbnail text)
    made: float = 0.0               # epoch the ad was made, to order a hook's ad sets
    ad_format: str | None = None    # video | image, from the job behind the ad set (None: not made by this tool)
    body: str | None = None         # the body video's name (None: the default body, or an image ad)

    @property
    def cpr(self) -> float | None:
        return self.spend / self.results if self.results else None


def is_win(a: AdSet, settings: Settings) -> bool:
    """Reached performing_min_results. Autopause checks every 2 hours, so an ad set that got that far was never
    stopped on the way; pausing it later (fatigue, a worse CPR) does not make it any less of a winner."""
    return a.results >= settings.ad_planning.performing_min_results


def is_decided(a: AdSet, settings: Settings) -> bool:
    """Paused (a loser unless it won first) or a win; a running ad set below the win bar has no outcome yet."""
    return not a.active or is_win(a, settings)


def smoothed_win_rate(wins: int, decided: int, settings: Settings) -> float:
    """Share of decided ad sets that won, pulled towards the configured prior while there is little history."""
    cfg = settings.ad_planning
    return (wins + cfg.prior_win_rate * cfg.prior_weight) / (decided + cfg.prior_weight)


def win_rate(adsets: list[AdSet], settings: Settings) -> tuple[float, int]:
    """(smoothed win rate, decided ad sets) of the given ad sets."""
    decided = [a for a in adsets if is_decided(a, settings)]
    return smoothed_win_rate(sum(is_win(a, settings) for a in decided), len(decided), settings), len(decided)


def format_stats(adsets: list[AdSet], settings: Settings) -> dict[str, dict]:
    """Per ad format: decided ad sets, wins and the smoothed win rate. Ad sets this tool did not make are left out."""
    stats = {}
    for fmt in (FORMAT_VIDEO, FORMAT_IMAGE):
        mine = [a for a in adsets if a.ad_format == fmt]
        rate, decided = win_rate(mine, settings)
        stats[fmt] = {"decided": decided, "wins": sum(is_win(a, settings) for a in mine if is_decided(a, settings)),
                      "win_rate": round(rate, 3)}
    return stats


def image_share(stats: dict[str, dict], settings: Settings) -> float:
    """Share of new-hook ads to make as image ads, purely on merit: the image win rate against both. A format that
    has had format_give_up_after decided ad sets without a single win gets none while the other format has won."""
    cfg = settings.ad_planning
    formats = list(cfg.get("formats", (FORMAT_VIDEO, FORMAT_IMAGE)))
    if FORMAT_IMAGE not in formats:
        return 0.0
    if FORMAT_VIDEO not in formats:
        return 1.0
    image, video = stats[FORMAT_IMAGE], stats[FORMAT_VIDEO]
    give_up = cfg.format_give_up_after
    if image["decided"] >= give_up and not image["wins"] and video["wins"]:
        return 0.0
    if video["decided"] >= give_up and not video["wins"] and image["wins"]:
        return 1.0
    return image["win_rate"] / (image["win_rate"] + video["win_rate"])


def format_reason(stats: dict[str, dict], share: float) -> str:
    v, i = stats[FORMAT_VIDEO], stats[FORMAT_IMAGE]
    outcome = (f"video ads: {v['wins']} of {v['decided']} decided ad sets won; image ads: {i['wins']} of {i['decided']}; ")
    if share == 0:
        return outcome + "so no image ads this time, only video ads"
    if share == 1:
        return outcome + "so no video ads this time, only image ads"
    return outcome + f"so {share:.0%} of new hooks are image ads"


def hook_outcomes(adsets: list[AdSet], settings: Settings, language_name: str | None = None,
                  limit: int | None = None) -> dict[str, list[dict]]:
    """The hooks that won (lowest cost per result first) and the ones paused before winning (newest first), per
    ad set, with both texts, format, body and numbers. `limit` caps each list."""
    limit = limit or settings.ad_planning.hook_history_limit
    mine = [a for a in adsets if a.hook and (language_name is None or a.language == language_name)]

    def row(a: AdSet) -> dict:
        return {"language": a.language, "spoken_text": a.hook[1], "thumbnail_text": a.hook[2], "ad_format": a.ad_format,
                "body": a.body, "results": a.results, "spend_inr": round(a.spend, 2),
                "cpr_inr": round(a.cpr, 2) if a.cpr else None}

    winners = sorted((a for a in mine if is_win(a, settings)), key=lambda a: (a.cpr, a.id))
    losers = sorted((a for a in mine if not a.active and not is_win(a, settings)), key=lambda a: (-a.made, a.id))
    return {"winners": [row(a) for a in winners[:limit]], "losers": [row(a) for a in losers[:limit]]}

