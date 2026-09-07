"""Topic catalog and sub-niche discovery for the console's run launcher.

Three sources, and the console labels which is which:

  finance and crime were built deliberately and delivered, so their
  sub-niches are read from niche_taxonomy with channels behind them and
  returned as `source: dataset`.

  Every other catalog topic has channels too, but only as residue swept up
  while crawling those two. Reading that back as a topic map produced
  "Kashmir Regional News" under News and "BCom Calcutta University Exam
  Prep" under Education -- accurate about the dataset, useless to a client
  choosing what to research. Those topics get a curated map instead.

  A topic nobody has run and nobody has curated is proposed by the model.

The last two are both `source: proposed`, because neither has counts behind
it. Presenting a guess with the authority of a measurement is how a client
picks a sub-niche that turns out to have four channels in it.

The model call uses the cheap tier and is capped, because this runs while
someone is typing.
"""

from __future__ import annotations

import re
from typing import Any

from src.llm.json_parse import complete_json
from src.db.connection import get_connection, put_connection

_MIN_CHANNELS_FOR_CATALOG = 20
_MAX_SUGGESTIONS = 14

SUGGEST_PROMPT = """You map a YouTube content topic into its distinct sub-niches.

Return ONLY a JSON array of 8-14 objects:
[{"name": "Bodycam / Police Incidents", "rationale": "one short clause"}]

Rules:
- A sub-niche is a distinct CONTENT FORMAT or SUBJECT within the topic that
  a viewer would recognise as its own kind of channel -- not an audience,
  not a country, not a company.
- Prefer sub-niches that plausibly have channels above 50,000 subscribers.
- Cover the topic's range, not just its most obvious corner.
- Name them the way a person would say them, in Title Case.
"""


# Topics whose coverage was built deliberately and shipped to a client. Only
# these have a dataset map worth showing: everywhere else the coverage is
# residue swept up while crawling these two, and reading it back as a topic
# map produces things like "Kashmir Regional News" under News.
DELIVERED_TOPICS = {"finance", "crime"}

# For every other catalog topic, the sub-niches an international audience
# would actually recognise. These are a map of the topic, not a readout of
# what the dataset holds, and the run is what turns them into coverage.
CURATED_SUBNICHES: dict[str, list[tuple[str, str]]] = {
    "entertainment": [
        ("Movie & TV Recaps", "full plot walkthroughs of films and series"),
        ("Reaction Videos", "creators watching and responding on camera"),
        ("Comedy Sketches", "scripted short-form comedy"),
        ("Anime Reviews & Explainers", "episode breakdowns and series analysis"),
        ("Horror Story Narration", "read-aloud horror over ambient visuals"),
        ("Celebrity News & Interviews", "profiles, sit-downs and industry news"),
        ("Viral Clip Commentary", "reacting to whatever is circulating that week"),
        ("Pranks & Social Experiments", "staged public-setting entertainment"),
        ("Animated Shorts & Web Series", "original animation made for the platform"),
        ("Behind the Scenes & Film Craft", "how films and shows get made"),
    ],
    "lifestyle": [
        ("Daily Life Vlogs", "a creator's ordinary week, filmed"),
        ("Home Organisation & Cleaning", "decluttering, routines and storage"),
        ("Minimalism & Slow Living", "spending less and owning less, deliberately"),
        ("Travel Vlogs", "destination-led travel filmed first person"),
        ("Home Workouts & Fitness", "training with little or no equipment"),
        ("Cooking & Meal Prep", "everyday recipes and batch cooking"),
        ("Fashion & Style", "outfit building, hauls and wardrobe advice"),
        ("Beauty & Skincare Routines", "product routines and technique tutorials"),
        ("Parenting & Family Life", "raising children, filmed candidly"),
        ("Interior Design & Home Tours", "decorating, renovating and walkthroughs"),
    ],
    "gaming": [
        ("Let's Plays & Walkthroughs", "playing a game through, with commentary"),
        ("Competitive Shooter Highlights", "ranked play, clips and improvement guides"),
        ("Minecraft Builds & Survival", "long-running worlds, builds and challenges"),
        ("Roblox Gameplay", "experience-hopping aimed at a younger audience"),
        ("Speedrunning", "record attempts and route explanation"),
        ("Game Reviews & First Impressions", "verdicts on new releases"),
        ("Mobile Gaming", "phone-first titles and tier lists"),
        ("Retro Gaming & Emulation", "older consoles, preservation and collecting"),
        ("Esports Coverage & Analysis", "tournaments, meta and team news"),
        ("Streamer Highlight Compilations", "the best of long live streams, cut short"),
        ("Gaming News & Leaks", "announcements, patches and rumours"),
        ("Racing & Simulation Games", "sim racing, flight and management sims"),
    ],
    "music": [
        ("Music Production Tutorials", "making tracks in a DAW, step by step"),
        ("Instrument Covers", "guitar, piano and drum covers of known songs"),
        ("Lo-fi & Study Beats", "long instrumental sets for working to"),
        ("Live Performance Sessions", "stripped-back sessions and gig recordings"),
        ("Music Theory Explained", "why songs work, for players and writers"),
        ("Song Reactions & Analysis", "first listens and close readings"),
        ("DJ Sets & Electronic Mixes", "continuous mixes, often filmed"),
        ("Vocal Coaching & Singing Tips", "technique, range and practice routines"),
        ("Ambient & Sleep Soundscapes", "long-form audio for sleep and focus"),
        ("Independent Artist Releases", "self-released music videos and EPs"),
    ],
    "history": [
        ("Ancient Civilisations", "Egypt, Rome, Mesopotamia and their successors"),
        ("Military History & Battles", "campaigns, tactics and their consequences"),
        ("Historical Documentaries", "long-form narrated history with archive"),
        ("Unsolved Historical Mysteries", "open questions and competing explanations"),
        ("Archaeology & New Discoveries", "digs, dating and what they change"),
        ("Biographies", "single lives told end to end"),
        ("Maps & Geopolitical History", "how borders and power moved"),
        ("Everyday Life in the Past", "food, work and housing across eras"),
        ("Historical Myths Debunked", "correcting what most people believe"),
        ("Modern World History", "the twentieth century and its aftermath"),
    ],
    "science_explainer": [
        ("Space & Astronomy", "missions, cosmology and what telescopes find"),
        ("Physics Explained", "from mechanics to quantum, for non-specialists"),
        ("Human Body & Medicine", "how bodies work and how treatment works"),
        ("Psychology & Behaviour", "why people do what they do, evidence-led"),
        ("Animated Science Explainers", "concepts carried by motion graphics"),
        ("Engineering & How Things Work", "machines, infrastructure and failure"),
        ("Climate & Earth Science", "the planet's systems and their measurement"),
        ("Mathematics Explained", "intuition first, proofs second"),
        ("Science News & Breakthroughs", "what was published and whether it holds"),
        ("Wildlife & Nature", "species, behaviour and habitat, filmed"),
    ],
    "technology": [
        ("Phone & Laptop Reviews", "hands-on verdicts on consumer hardware"),
        ("PC Building & Components", "parts, builds and benchmark comparisons"),
        ("Programming Tutorials", "learning a language or framework by building"),
        ("AI Tools & Workflows", "what the current tools can and cannot do"),
        ("Cybersecurity Explained", "threats, defence and privacy in practice"),
        ("Software Tips & Productivity", "getting more out of everyday tools"),
        ("Tech News & Product Launches", "announcements and what they mean"),
        ("Smart Home & Networking", "setting up a home that mostly works"),
        ("Repair & Teardown", "opening devices, fixing and assessing them"),
        ("Developer Careers", "interviews, portfolios and getting hired"),
    ],
    "education": [
        ("Exam Preparation", "structured revision for major exams"),
        ("Language Learning", "grammar, vocabulary and listening practice"),
        ("Mathematics Lessons", "school and university maths, worked through"),
        ("Science Lessons", "physics, chemistry and biology by syllabus"),
        ("Study Skills & Productivity", "how to revise, plan and keep going"),
        ("English Proficiency Tests", "IELTS, TOEFL and academic English"),
        ("University & Career Guidance", "choosing courses and what follows them"),
        ("Coding for Beginners", "first programs, for people starting cold"),
        ("Study With Me", "long real-time sessions to work alongside"),
        ("Adult Learning & Reskilling", "changing field later in a career"),
    ],
    "news": [
        ("World News & Current Affairs", "the day's events, reported"),
        ("Geopolitics Explained", "why states act as they do"),
        ("Business & Economic News", "markets, policy and what moves them"),
        ("Investigative Journalism", "long-running reporting on one story"),
        ("News Analysis & Commentary", "argued positions on the week's news"),
        ("Conflict Reporting", "on-the-ground coverage of wars and crises"),
        ("Fact-Checking & Media Literacy", "testing claims and how they spread"),
        ("Science & Technology News", "research and industry, for a general audience"),
        ("Daily News Briefings", "short, regular catch-up formats"),
        ("Documentary Journalism", "single-subject reported films"),
    ],
    "automotive": [
        ("Car Reviews & Road Tests", "driving impressions and verdicts"),
        ("DIY Repair & Maintenance", "fixing your own car, filmed at the bench"),
        ("Modification & Tuning", "power, handling and appearance builds"),
        ("Classic Cars & Restoration", "long restoration projects, start to finish"),
        ("Motorcycles & Riding", "bikes, gear and road craft"),
        ("Electric Vehicles", "ownership, range, charging and running costs"),
        ("Detailing & Car Care", "cleaning, correction and protection"),
        ("Motorsport", "racing series, technical rules and analysis"),
        ("Buying Guides & Running Costs", "what to buy and what it costs to keep"),
        ("Off-Road & Overlanding", "trails, vehicle prep and long expeditions"),
    ],
}


def _key(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (topic or "").strip().lower()).strip("_")


def catalog() -> list[dict[str, Any]]:
    """Topics the dataset already has real coverage for."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT nt.parent_category,
                      COUNT(DISTINCT nt.niche_id)   AS niches,
                      COUNT(DISTINCT cn.channel_id) AS channels
               FROM niche_taxonomy nt
               LEFT JOIN channel_niches cn
                      ON cn.niche_id = nt.niche_id AND cn.is_primary
               WHERE nt.parent_category IS NOT NULL
               GROUP BY 1
               HAVING COUNT(DISTINCT cn.channel_id) >= %s
               ORDER BY 3 DESC""",
            (_MIN_CHANNELS_FOR_CATALOG,),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        put_connection(conn)

    return [
        {
            "id": r[0],
            "label": r[0].replace("_", " ").title(),
            "niche_count": r[1],
            "channel_count": r[2],
            "has_dataset": True,
        }
        for r in rows
    ]


def _dataset_subniches(topic: str) -> list[dict[str, Any]]:
    """Sub-niches with channels actually behind them, biggest first."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT nt.niche_name, COUNT(DISTINCT cn.channel_id) AS channels
               FROM niche_taxonomy nt
               JOIN channel_niches cn
                 ON cn.niche_id = nt.niche_id AND cn.is_primary
               WHERE lower(nt.parent_category) = lower(%s)
               GROUP BY 1
               HAVING COUNT(DISTINCT cn.channel_id) > 0
               ORDER BY 2 DESC
               LIMIT %s""",
            (topic, _MAX_SUGGESTIONS),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        put_connection(conn)

    return [
        {
            "name": r[0].replace("_", " ").title(),
            "slug": r[0],
            "channel_count": r[1],
            "source": "dataset",
            # No rationale: the only thing there was to say here was the
            # channel count, which the console deliberately does not show.
            "rationale": "",
        }
        for r in rows
    ]


def _proposed_subniches(topic: str) -> list[dict[str, Any]]:
    """Model-proposed sub-niches for a topic with no coverage yet."""
    from src.llm.cascade import complete_tier

    try:
        parsed, _ = complete_json(
            complete_tier, "cheap", f"Topic: {topic}", SUGGEST_PROMPT, expect="array"
        )
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []

    out: list[dict[str, Any]] = []
    for item in parsed[:_MAX_SUGGESTIONS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "slug": re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"),
                "channel_count": None,
                "source": "proposed",
                "rationale": str(item.get("rationale") or "").strip(),
            }
        )
    return out


def suggest(topic: str) -> dict[str, Any]:
    """Sub-niches for a topic: measured where the coverage was built on
    purpose, curated where it was not, and model-proposed for anything new."""
    topic = (topic or "").strip()
    if not topic:
        return {"topic": topic, "source": "none", "subniches": []}

    key = _key(topic)

    if key in DELIVERED_TOPICS:
        known = _dataset_subniches(key)
        if known:
            return {
                "topic": topic,
                "source": "dataset",
                "note": (
                    "These sub-niches already have channels behind them, "
                    "mapped by the runs that produced this vertical."
                ),
                "subniches": known,
            }

    curated = CURATED_SUBNICHES.get(key)
    if curated:
        return {
            "topic": topic,
            "source": "proposed",
            "note": (
                "A map of the topic rather than a readout of what we already "
                "hold. The run is what turns these into real coverage."
            ),
            "subniches": [
                {
                    "name": name,
                    "slug": _key(name),
                    "channel_count": None,
                    "source": "proposed",
                    "rationale": why,
                }
                for name, why in curated
            ],
        }

    proposed = _proposed_subniches(topic)
    return {
        "topic": topic,
        "source": "proposed" if proposed else "none",
        "note": (
            "A first read on a topic nobody has run yet, proposed by the "
            "model. A run is what turns these into counts."
        ),
        "subniches": proposed,
    }
