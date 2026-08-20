"""v3 taxonomy seeds — metadata-tier only per plan §7.1.

Called once by ensure_schema after migrations so the factor and niche
taxonomies are never empty when enrichment nodes try to match against them.
INSERT ... ON CONFLICT DO NOTHING makes repeated calls idempotent.

No content-level factors (hook structure, pacing, editing rhythm) — those
require L4, which stays deferred. Every factor here is metadata-observable.
"""

from __future__ import annotations


SUCCESS_FACTORS: list[dict[str, str]] = [
    # title_metadata
    {"code": "title_high_word_count", "label": "High word count titles", "group": "title_metadata",
     "desc": "Titles above channel-average word count correlate with outlier performance"},
    {"code": "title_contains_number", "label": "Title contains numbers/lists", "group": "title_metadata",
     "desc": "Numbered-list or quantified titles outperform narrative titles"},
    {"code": "title_is_question", "label": "Question-format titles", "group": "title_metadata",
     "desc": "Titles framed as questions drive above-average click-through"},
    {"code": "title_all_caps_emphasis", "label": "ALL-CAPS emphasis words", "group": "title_metadata",
     "desc": "Strategic all-caps keywords in titles correlate with higher views"},
    {"code": "title_emoji_usage", "label": "Emoji in titles", "group": "title_metadata",
     "desc": "Emoji usage patterns correlate with higher engagement"},
    {"code": "title_sentence_case", "label": "Sentence-case titles", "group": "title_metadata",
     "desc": "Natural sentence-case titles outperform title-case in certain niches"},

    # thumbnail
    {"code": "thumbnail_high_text", "label": "High text-density thumbnails", "group": "thumbnail",
     "desc": "Thumbnails with prominent text overlay correlate with outlier views"},
    {"code": "thumbnail_faceless", "label": "Faceless thumbnail style", "group": "thumbnail",
     "desc": "Faceless thumbnails with strong visual hooks outperform face-centric in faceless niches"},
    {"code": "thumbnail_face_present", "label": "Face-present thumbnails", "group": "thumbnail",
     "desc": "Human face in thumbnail correlates with higher engagement in personality-driven niches"},
    {"code": "thumbnail_branded_style", "label": "Consistent branded thumbnail style", "group": "thumbnail",
     "desc": "Channels with visually consistent thumbnails across uploads retain viewers better"},

    # format
    {"code": "animated_explainer_format", "label": "Animated explainer format", "group": "format",
     "desc": "Scripted animated explainers with voiceover show outlier performance"},
    {"code": "talking_head_format", "label": "Talking head direct-address", "group": "format",
     "desc": "Direct-address talking head with jump cuts correlates with high retention"},
    {"code": "documentary_narration_format", "label": "Documentary narrative format", "group": "format",
     "desc": "Long-form documentary-style narration outperforms in true-crime/history niches"},
    {"code": "compilation_format", "label": "Clip compilation format", "group": "format",
     "desc": "Curated clip compilations with commentary in specific sub-niches"},
    {"code": "shorts_format", "label": "Short-form vertical content", "group": "format",
     "desc": "Channels winning primarily through Shorts format rather than long-form"},

    # cadence
    {"code": "consistent_upload_cadence", "label": "Consistent upload schedule", "group": "cadence",
     "desc": "Regular upload intervals with low variance correlate with channel growth"},
    {"code": "high_upload_frequency", "label": "High-frequency uploads", "group": "cadence",
     "desc": "2+ uploads per week correlate with faster accumulation of outlier videos"},
    {"code": "weekday_targeting", "label": "Specific weekday upload pattern", "group": "cadence",
     "desc": "Consistently publishing on specific days of the week"},

    # niche_fit
    {"code": "underserved_subniche", "label": "Underserved sub-niche positioning", "group": "niche_fit",
     "desc": "Channel targets a sub-niche with high search volume relative to competing channels"},
    {"code": "cross_niche_hybrid", "label": "Cross-niche hybrid content", "group": "niche_fit",
     "desc": "Channels blending two adjacent niches show faster audience acquisition"},
    {"code": "newsjacking_responsive", "label": "Rapid news/trend response", "group": "niche_fit",
     "desc": "Channels that publish within hours of major news correlate with outlier spikes"},
    {"code": "evergreen_catalog_building", "label": "Evergreen search-engine content", "group": "niche_fit",
     "desc": "Channels building a search-optimized evergreen catalog accumulate views over time"},

    # monetization
    {"code": "affiliate_revenue_model", "label": "Affiliate-link monetization", "group": "monetization",
     "desc": "Description affiliate links indicate a tested revenue model"},
    {"code": "sponsorship_presence", "label": "Sponsor-integration signals", "group": "monetization",
     "desc": "Sponsorship mentions in descriptions indicate commercial viability"},
    {"code": "membership_community", "label": "Community/membership monetization", "group": "monetization",
     "desc": "Patreon/membership mentions indicate a recurring-revenue model"},

    # other
    {"code": "high_engagement_rate", "label": "Above-median engagement rate", "group": "other",
     "desc": "Channels with engagement rate above their cluster median"},
    {"code": "high_evergreen_rating", "label": "High evergreen content rating", "group": "other",
     "desc": "Channels whose videos maintain views-per-day comparable to their own archive"},
    {"code": "rapid_growth_velocity", "label": "Fast subscriber growth velocity", "group": "other",
     "desc": "Subscriber growth rate above cluster peers' average"},
    {"code": "breakout_video_pattern", "label": "Recurring breakout videos", "group": "other",
     "desc": "Multiple videos with outlier_score >= 3.0, not just one lucky spike"},
    {"code": "niche_authority_position", "label": "Niche authority positioning", "group": "other",
     "desc": "Channel appears as featured_channel edge on multiple peers in its cluster"},
]

FAILURE_FACTORS: list[dict[str, str]] = [
    # title_metadata
    {"code": "low_title_word_count", "label": "Below-median title word count", "group": "title_metadata",
     "desc": "Titles consistently shorter than cluster median, missing search/keyword opportunities"},
    {"code": "no_quantification", "label": "No numbers or lists in titles", "group": "title_metadata",
     "desc": "Titles never use numbered lists or quantified claims"},
    {"code": "weak_title_hooks", "label": "Non-hooking title patterns", "group": "title_metadata",
     "desc": "Descriptive or diary-style titles without a click-driving hook element"},
    {"code": "inconsistent_title_case", "label": "Inconsistent title capitalization", "group": "title_metadata",
     "desc": "Titles switching between case styles across uploads, eroding brand consistency"},
    {"code": "title_stuffing", "label": "Keyword-stuffed or spammy titles", "group": "title_metadata",
     "desc": "Titles packed with comma-separated keywords rather than a coherent hook"},

    # thumbnail
    {"code": "no_thumbnail_overlay", "label": "No text overlay on thumbnails", "group": "thumbnail",
     "desc": "Thumbnails lack text overlay in niches where text-augmented thumbs are the norm"},
    {"code": "inconsistent_thumbnail_style", "label": "Inconsistent thumbnail design", "group": "thumbnail",
     "desc": "Wildly varying thumbnail styles across uploads, no visual brand identity"},
    {"code": "low_res_or_ai_thumb", "label": "Low-effort or generic AI thumbnails", "group": "thumbnail",
     "desc": "Thumbnails appear auto-generated or low-resolution compared to cluster peers"},
    {"code": "missing_face_where_expected", "label": "No face in personality niche", "group": "thumbnail",
     "desc": "Absence of human face in thumbnails for niches where face-present is standard"},
    {"code": "cluttered_thumb", "label": "Overly cluttered thumbnails", "group": "thumbnail",
     "desc": "Too many competing visual elements in thumbnails"},

    # format
    {"code": "no_clear_format", "label": "No consistent video format", "group": "format",
     "desc": "Channel switches randomly between vlog/tutorial/list/clip formats"},
    {"code": "unscripted_runway_content", "label": "Unscripted long-runway content", "group": "format",
     "desc": "Long-form content that takes minutes to reach the core topic"},
    {"code": "outdated_format", "label": "Outdated content format", "group": "format",
     "desc": "Content format that underperforms current cluster norms"},
    {"code": "no_shorts_strategy", "label": "No Shorts presence in Shorts-heavy niche", "group": "format",
     "desc": "Absence of Shorts content in a niche where Shorts drives discovery"},
    {"code": "only_shorts_no_longform", "label": "Shorts-only without long-form catalog", "group": "format",
     "desc": "Channel has only Shorts with no long-form content for watch-time accumulation"},

    # cadence
    {"code": "irregular_upload_schedule", "label": "Erratic upload schedule", "group": "cadence",
     "desc": "High variance in upload intervals, no predictable publishing rhythm"},
    {"code": "long_content_gaps", "label": "Long gaps between uploads", "group": "cadence",
     "desc": "Extended periods (weeks/months) with zero uploads between active periods"},
    {"code": "content_burst_and_abandon", "label": "Burst-publish then abandon", "group": "cadence",
     "desc": "Drops multiple videos in one period then goes silent for extended duration"},
    {"code": "low_overall_frequency", "label": "Sub-monthly upload frequency", "group": "cadence",
     "desc": "Fewer than one upload per month, insufficient to build an algorithmic presence"},

    # niche_fit
    {"code": "oversaturated_niche", "label": "Oversaturated niche positioning", "group": "niche_fit",
     "desc": "Channel competing in a niche with very high established creator density"},
    {"code": "no_niche_focus", "label": "No clear niche positioning", "group": "niche_fit",
     "desc": "Content spanning multiple unrelated topics, no discernible target audience"},
    {"code": "news_dependency", "label": "Over-reliance on news/trend content", "group": "niche_fit",
     "desc": "Channel built entirely on trending/news topics with low evergreen catalog value"},
    {"code": "declining_niche", "label": "Declining niche interest", "group": "niche_fit",
     "desc": "Niche showing declining aggregate view velocity across cluster peers"},
    {"code": "regional_language_barrier", "label": "Language-limited audience", "group": "niche_fit",
     "desc": "Content in a language with addressable-audience ceiling well below US-English peers"},

    # monetization
    {"code": "no_monetization_signal", "label": "No monetization signals present", "group": "monetization",
     "desc": "No affiliate, sponsor, or membership signals detectable in description"},
    {"code": "low_cpm_niche", "label": "Historically low-CPM content category", "group": "monetization",
     "desc": "Content in a niche with well-documented low advertising CPM rates"},
    {"code": "no_product_ecosystem", "label": "No product or service tie-in", "group": "monetization",
     "desc": "Channel format doesn't naturally lead to a sellable product, course, or service"},

    # other
    {"code": "low_engagement_rate", "label": "Below-median engagement rate", "group": "other",
     "desc": "Engagement rate consistently below cluster peer median"},
    {"code": "short_shelf_life", "label": "Short content shelf life", "group": "other",
     "desc": "Videos lose views-per-day rapidly, low evergreen_score relative to cluster"},
    {"code": "dead_or_spam_channel", "label": "Likely dead or spam channel", "group": "other",
     "desc": "Zero recent uploads, spam-pattern titles, or subscriber-to-view ratio anomaly"},
    {"code": "low_outlier_rate", "label": "No breakout video history", "group": "other",
     "desc": "No videos with outlier_score >= 2.0, no sign of audience breakthrough"},
    {"code": "subscriber_stagnation", "label": "Stagnant subscriber growth", "group": "other",
     "desc": "Subscriber count unchanged or declining across observable runs"},
]

# Starter niches from common YouTube verticals. Replaced by real discovery data
# as runs accumulate — these are just so the taxonomy is never empty on first run.
NICHE_SEEDS: list[dict[str, str]] = [
    {"name": "personal_finance_budgeting", "cat": "finance", "desc": "Personal budgeting, saving strategies, and financial literacy content", "evergreen": True},
    {"name": "investing_stock_market", "cat": "finance", "desc": "Stock market analysis, investing strategies, and portfolio management", "evergreen": False},
    {"name": "crypto_web3", "cat": "finance", "desc": "Cryptocurrency, DeFi, NFT, and Web3 explainers and news", "evergreen": False},
    {"name": "true_crime_documentary", "cat": "crime", "desc": "Long-form true-crime documentary, case analysis, and investigation narrative", "evergreen": True},
    {"name": "body_cam_footage", "cat": "crime", "desc": "Police body-cam footage compilation and reaction/analysis content", "evergreen": True},
    {"name": "interrogation_analysis", "cat": "crime", "desc": "Police interrogation footage breakdown and behavioral/deception analysis", "evergreen": True},
    {"name": "finance_documentary", "cat": "finance", "desc": "Long-form documentary narrative covering financial history, market collapses, fraud cases, and economic deep-dives", "evergreen": True},
    {"name": "world_history", "cat": "history", "desc": "Historical documentary, event deep-dives, and era-analysis content", "evergreen": True},
    {"name": "ancient_civilizations", "cat": "history", "desc": "Ancient history, archaeology findings, and civilization deep-dives", "evergreen": True},
    {"name": "science_explainers_physics", "cat": "science_explainer", "desc": "Physics concepts, space exploration, and hard-science explainers", "evergreen": True},
    {"name": "biology_medicine", "cat": "science_explainer", "desc": "Biology, medical science, and health-research explainers", "evergreen": True},
    {"name": "ai_coding_tools", "cat": "technology", "desc": "AI-assisted coding tools, Copilot, and developer workflow content", "evergreen": False},
    {"name": "tech_review_gadgets", "cat": "technology", "desc": "Consumer tech reviews, gadget comparisons, and hardware analysis", "evergreen": False},
    {"name": "retro_handheld_emulation", "cat": "gaming", "desc": "Retro gaming handheld reviews, emulation guides, and custom firmware", "evergreen": True},
    {"name": "game_analysis_essay", "cat": "gaming", "desc": "Long-form video-game analysis, critique, and design essays", "evergreen": True},
    {"name": "home_cooking_recipes", "cat": "lifestyle", "desc": "Recipe tutorials, cooking techniques, and food culture content", "evergreen": True},
    {"name": "productivity_self_improvement", "cat": "lifestyle", "desc": "Productivity systems, self-improvement frameworks, and habit-building", "evergreen": True},
    {"name": "fitness_training", "cat": "lifestyle", "desc": "Workout routines, training methodology, and fitness education", "evergreen": True},
    {"name": "travel_vlog_adventure", "cat": "lifestyle", "desc": "Travel vlogs, destination guides, and adventure documentary", "evergreen": True},
    {"name": "car_review_automotive", "cat": "automotive", "desc": "Car reviews, automotive engineering explainers, and enthusiast content", "evergreen": True},
    {"name": "music_production_tutorial", "cat": "music", "desc": "Music production tutorials, DAW workflows, and sound design", "evergreen": True},
    {"name": "film_analysis_cinematography", "cat": "entertainment", "desc": "Film analysis, cinematography breakdowns, and directorial deep-dives", "evergreen": True},
]


def seed_taxonomies(conn) -> int:
    """Populate factor and niche taxonomies. Idempotent — skips existing rows.

    Returns number of new rows inserted (0 if already seeded).
    """
    inserted = 0
    cur = conn.cursor()
    try:
        for table, rows in [
            ("success_factor_taxonomy", SUCCESS_FACTORS),
            ("failure_factor_taxonomy", FAILURE_FACTORS),
        ]:
            for r in rows:
                cur.execute(
                    f"""INSERT INTO {table} (factor_code, factor_label, factor_group, description)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (factor_code) DO NOTHING""",
                    (r["code"], r["label"], r["group"], r["desc"]),
                )
                if cur.rowcount:
                    inserted += 1
        conn.commit()
        for n in NICHE_SEEDS:
            cur.execute(
                """INSERT INTO niche_taxonomy (niche_name, parent_category, description, is_evergreen_prone)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (niche_name) DO NOTHING""",
                (n["name"], n["cat"], n["desc"], n["evergreen"]),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return inserted