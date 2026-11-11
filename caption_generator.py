"""Short, transcript-grounded YouTube Shorts metadata in the spoken language."""
from __future__ import annotations

import re
import hashlib
from collections import Counter

STOPWORDS = set((
    "a an the and or but if then is are was were be been being to of in on at for with from by as it its "
    "this that i you he she we they them my your our their what who when where why how just really very "
    "about into out up down so do does did not no yes can could would should have has had get got make made "
    "like there here و في من على عن الى إلى هذا هذه ان أن كان كانت هو هي هم انا أنا انت أنت احنا اللي "
    "شنو شلون ليش كيف ايش مع على مو ما لا بس لكن والله يعني جدا جداً اليوم"
).split())

BROAD_TAGS = {
    "gaming": "#gaming", "game": "#gaming", "gta": "#gaming", "gta5": "#gaming",
    "football": "#football", "soccer": "#football", "nba": "#basketball", "basketball": "#basketball",
    "music": "#music", "song": "#music", "reaction": "#reaction", "react": "#reaction",
    "funny": "#comedy", "laugh": "#comedy", "comedy": "#comedy", "podcast": "#podcast",
    "tech": "#tech", "ai": "#AI", "food": "#food", "cooking": "#cooking", "news": "#news",
    "fitness": "#fitness", "travel": "#travel", "money": "#finance", "cars": "#cars",
    "car": "#cars", "lowrider": "#cars", "lowriders": "#cars",
}
NICHE_TAGS = {
    "gta": "#GTA5", "gta5": "#GTA5", "grand": "#GTA5", "lowrider": "#Lowrider",
    "lowriders": "#Lowrider", "car": "#Cars", "cars": "#Cars", "streamer": "#Streamer",
    "stream": "#Streamer", "twitch": "#Streamer", "nba": "#NBA", "basketball": "#NBA",
    "soccer": "#Soccer", "football": "#Football", "podcast": "#PodcastClips",
    "reaction": "#ReactionVideo", "react": "#ReactionVideo", "recipe": "#CookingTips",
    "cooking": "#CookingTips", "ai": "#AITools", "technology": "#TechTips",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[#@]?[\w]+(?:['’][\w]+)?", text.lower(), flags=re.UNICODE)


def _clean_hook(transcript: str) -> str:
    words = re.findall(r"\S+", transcript.strip())
    hook = " ".join(words[:11]).strip(" .!?؟-—")
    if len(hook) > 90:
        hook = hook[:87].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return hook


def _short_title(hook: str) -> str:
    value = re.sub(r"\s+", " ", hook).strip(" -—")
    sentences = [part.strip(" -—") for part in re.split(r"(?<=[.!?؟])\s+", value) if part.strip()]
    complete = next((part for part in sentences if len(part) >= 12), "")
    title = complete or value
    if len(title) > 72:
        title = title[:69].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return title


def _topic_tags(text: str, source_channel: str = "", source_title: str = "") -> list[str]:
    value = re.sub(r"\s+", " ", f"{text} {source_title} {source_channel}").lower()
    category_rules = (
        (r"\b(gta\s*5|gta|grand theft auto|lowrider|lowriders)\b", "#Gaming"),
        (r"\b(spider[- ]?man|spiderman|halloween|superhero)\b", "#Entertainment"),
        (r"\b(football|soccer|ronaldo|goal)\b|كرة القدم|رونالدو", "#Football"),
        (r"\b(nba|basketball)\b|كرة السلة", "#Basketball"),
        (r"\b(mcdonald|burger|food|restaurant|recipe|cooking)\b|طبخ|وصفة", "#Food"),
        (r"\b(podcast)\b", "#Podcast"),
        (r"\b(prank|challenge|funny|comedy|laugh)\b|مقلب|ضحك", "#Comedy"),
        (r"\b(twitch|streamer|streaming)\b|بث|ستريمر", "#Entertainment"),
    )
    broad = next((tag for pattern, tag in category_rules if re.search(pattern, value)), "#Entertainment")

    channel = source_channel.lower()
    channel_tags = (
        (("ishowspeed", "speed", "سبيد"), "#IShowSpeed"),
        (("kaicenat", "kai cenat", "kai", "كاي"), "#KaiCenat"),
        (("stableronaldo", "ronaldo", "رونالدو"), "#StableRonaldo"),
    )
    topic_tags = (
        (("gta", "grand theft auto"), "#GTA5"),
        (("lowrider", "lowriders"), "#Lowrider"),
        (("spider-man", "spiderman"), "#SpiderMan"),
        (("halloween",), "#Halloween"),
        (("football", "soccer", "ronaldo"), "#Football"),
        (("basketball", "nba"), "#NBA"),
        (("mcdonald",), "#McDonalds"),
        (("podcast",), "#PodcastClips"),
        (("twitch", "streamer", "streaming"), "#StreamerClips"),
        (("prank",), "#Prank"),
        (("challenge",), "#Challenge"),
        (("reaction", "react"), "#Reaction"),
        (("ai", "artificial intelligence"), "#AITools"),
    )
    niche: list[str] = []
    for terms, tag in channel_tags:
        if any(term in channel for term in terms):
            niche.append(tag)
            break
    for terms, tag in topic_tags:
        if any(term in value for term in terms) and tag.lower() != broad.lower():
            niche.append(tag)

    # Use nouns from the source title only when a known topic mapping is absent.
    if not niche:
        title_words = Counter(word.strip("#@'’.,!?؟:;()[]{}\"-") for word in _words(source_title))
        excluded = STOPWORDS | {"says", "said", "something", "anything", "gonna", "yeah", "okay",
                                "going", "look", "looks", "hold", "bro", "guys", "today", "video"}
        for word, _ in title_words.most_common():
            if len(word) < 4 or word in excluded or not re.fullmatch(r"[a-z0-9]+", word):
                continue
            niche.append("#" + word.title())
            if len(niche) == 2:
                break
    if not niche:
        niche = ["#CreatorClips"]

    result: list[str] = []
    for tag in [broad, *niche[:2], "#Shorts"]:
        if tag.lower() not in {item.lower() for item in result}:
            result.append(tag)
    return result


def generate_metadata(transcript: str, source_title: str = "", *, hook: str = "",
                      visual_cue: str = "", moment_reason: str = "",
                      start_seconds: float | None = None,
                      end_seconds: float | None = None,
                      source_channel: str = "") -> dict[str, object]:
    """Build a concise hook, CTA, and no-more-than-four relevant hashtags.

    Hashtags are content-derived. #Shorts labels the format; no live-trend claim
    is made because this local pipeline does not query trend data.
    """
    clean = re.sub(r"\s+", " ", transcript).strip()
    hook_text = re.sub(r"\s+", " ", (hook or _clean_hook(clean))).strip()
    title = _short_title(hook_text or re.sub(r"\s+", " ", source_title).strip() or "A short moment")

    is_arabic = bool(re.search(r"[\u0600-\u06ff]", clean))
    if is_arabic:
        caption_options = ("تسوي نفس الشي لو لا؟ 👀", "منو كان رأيه أقوى؟ 🤔",
                           "شنو كان ردّك بهالموقف؟ 👀", "تصرفه كان بمحله لو بالغ؟ 🤔")
        cta = "اكتب رأيك بالتعليقات."
    else:
        reason = moment_reason.lower()
        if any(term in reason for term in ("conflict", "contradiction", "صدام", "تناقض")):
            caption_options = ("Who was right here? 👀", "Who had the better point? 🤔")
        elif "?" in hook_text:
            caption_options = ("What would you say here? 👀", "Would you have answered that? 🤔",
                               "What's your take? 👀")
        else:
            caption_options = ("Would you have done the same? 👀", "Did you see that coming? 😳",
                               "Fair reaction or too much? 🤔")
        cta = "Tell me why in the comments."

    seed = hashlib.sha256(f"{hook_text}|{clean[:120]}".encode("utf-8")).digest()
    caption = caption_options[int.from_bytes(seed[:2], "big") % len(caption_options)]
    tags = _topic_tags(clean, source_channel, source_title)
    description = "\n".join(part for part in (caption, cta, " ".join(tags)) if part)
    return {
        "title": title,
        "hook": hook_text,
        "visual_cue": visual_cue,
        "moment_reason": moment_reason,
        "caption": caption,
        "cta": cta,
        "hashtags": tags,
        "source_channel": source_channel,
        "description": description[:5000],
        "moment_start_seconds": start_seconds,
        "moment_end_seconds": end_seconds,
    }
