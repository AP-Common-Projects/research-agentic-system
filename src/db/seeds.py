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

    # --- v4 expanded Finance niches (brief §2) ---
    {"name": "bonds", "cat": "finance", "desc": "Government and corporate bonds, fixed income, yield analysis", "evergreen": False},
    {"name": "currencies_forex", "cat": "finance", "desc": "Currency trading, forex market analysis, exchange rate strategy", "evergreen": False},
    {"name": "etf_passive_investing", "cat": "finance", "desc": "ETF investing, passive index strategies, fund comparison", "evergreen": True},
    {"name": "dividend_investing", "cat": "finance", "desc": "Dividend stock analysis, income investing, dividend growth", "evergreen": True},
    {"name": "value_investing", "cat": "finance", "desc": "Value investing methodology, fundamental analysis, intrinsic value", "evergreen": True},
    {"name": "quant_systematic_investing", "cat": "finance", "desc": "Quantitative investing, algorithmic trading, systematic strategies", "evergreen": False},
    {"name": "options_trading", "cat": "finance", "desc": "Options strategies, derivatives, volatility trading", "evergreen": False},
    {"name": "trading_education", "cat": "finance", "desc": "Trading education, chart analysis, technical indicators", "evergreen": True},
    {"name": "macro_investing", "cat": "finance", "desc": "Macroeconomic investing, central bank policy, global market trends", "evergreen": False},
    {"name": "finance_gen_z", "cat": "finance", "desc": "Finance for Gen Z — investing, saving, and money content for younger audiences", "evergreen": True},
    {"name": "finance_young_adults", "cat": "finance", "desc": "Financial literacy and money management for young adults (20s-30s)", "evergreen": True},
    {"name": "finance_women", "cat": "finance", "desc": "Finance and investing content tailored to women audiences", "evergreen": True},
    {"name": "finance_immigrants_expats", "cat": "finance", "desc": "Cross-border finance, expat tax, immigrant wealth building", "evergreen": True},
    {"name": "small_business_finance", "cat": "finance", "desc": "Small business financial management, accounting, and growth strategy", "evergreen": True},
    {"name": "creator_economy_finance", "cat": "finance", "desc": "Creator monetization, content business finance, platform revenue", "evergreen": False},
    {"name": "luxury_wealth_economics", "cat": "finance", "desc": "Luxury markets, wealth economics, high-net-worth content", "evergreen": False},
    {"name": "ai_finance", "cat": "finance", "desc": "AI applications in finance — tools, analysis, and automation", "evergreen": False},
    {"name": "ai_economy", "cat": "finance", "desc": "AI's impact on the economy, labor markets, and productivity", "evergreen": False},
    {"name": "ai_investing", "cat": "finance", "desc": "AI-driven investing, robo-advisors, automated portfolio management", "evergreen": False},
    {"name": "stock_market_stories", "cat": "finance", "desc": "Stock market narrative content — company stories, market history, drama", "evergreen": True},
    {"name": "financial_scams_fraud", "cat": "finance", "desc": "Financial scams, fraud investigations, Ponzi schemes, and consumer protection", "evergreen": True},
    {"name": "geopolitics_finance", "cat": "finance", "desc": "Intersection of geopolitics and financial markets — sanctions, trade wars, currency crises", "evergreen": False},
    {"name": "debt_credit", "cat": "finance", "desc": "Debt management, credit scores, loan strategies, and credit education", "evergreen": True},
    {"name": "mortgage_housing_finance", "cat": "finance", "desc": "Mortgage strategies, housing market analysis, real estate finance", "evergreen": True},
    {"name": "retirement", "cat": "finance", "desc": "Retirement planning, 401k, IRA, pension, and retirement lifestyle", "evergreen": True},
    {"name": "tax_education", "cat": "finance", "desc": "Tax planning, tax strategy, and tax education content", "evergreen": True},
    {"name": "insurance", "cat": "finance", "desc": "Insurance products, risk management, and insurance education", "evergreen": True},
    {"name": "personal_finance_psychology", "cat": "finance", "desc": "Behavioral finance, money psychology, spending habits", "evergreen": True},
    {"name": "behavioral_finance", "cat": "finance", "desc": "Behavioral economics applied to finance — biases, decision-making, nudges", "evergreen": True},
    {"name": "financial_independence_fire", "cat": "finance", "desc": "FIRE movement — financial independence, retire early strategies", "evergreen": True},
    {"name": "wealth_building", "cat": "finance", "desc": "Long-term wealth building, asset accumulation, net worth growth", "evergreen": True},
    {"name": "financial_markets_explainers", "cat": "finance", "desc": "Financial markets explained — how markets work, market structure, beginners", "evergreen": True},
    {"name": "market_analysis", "cat": "finance", "desc": "Market analysis, sector rotation, technical/fundamental market commentary", "evergreen": False},
    {"name": "economic_storytelling", "cat": "finance", "desc": "Narrative-driven economic content — storytelling about economic events and trends", "evergreen": True},
    {"name": "financial_history", "cat": "finance", "desc": "History of finance — market crashes, financial innovations, historical cases", "evergreen": True},
    {"name": "economic_history", "cat": "finance", "desc": "Economic history — great depressions, hyperinflation, economic transformations", "evergreen": True},
    {"name": "company_breakdowns", "cat": "finance", "desc": "Company deep-dives, business model analysis, corporate strategy breakdowns", "evergreen": True},
    {"name": "consumer_economics", "cat": "finance", "desc": "Consumer economics — inflation impact, purchasing power, household finance at macro scale", "evergreen": False},

    # --- v4 expanded crime niches ---
    {"name": "police_investigation_documentary", "cat": "crime", "desc": "Long-form police investigation documentary, detective work, case breakdowns", "evergreen": True},
    {"name": "interrogation_criminal_psychology", "cat": "crime", "desc": "Interrogation analysis, criminal psychology, suspect interview breakdowns", "evergreen": True},
    {"name": "cold_case_unsolved", "cat": "crime", "desc": "Cold case investigations, unsolved mysteries, dormant case re-examinations", "evergreen": True},
    {"name": "digital_evidence_internet_crime", "cat": "crime", "desc": "Digital evidence analysis, internet crime, cyber-investigation, social-media-based crime", "evergreen": False},
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
        # v4: seed primary_niche_groups and cohort_definitions
        for g in V4_PRIMARY_NICHE_GROUPS:
            cur.execute(
                """INSERT INTO primary_niche_groups (vertical, group_label, description)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (vertical, group_label) DO NOTHING""",
                (g["vertical"], g["label"], g["desc"]),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
        for p in V4_NICHE_ADJACENCY:
            cur.execute(
                """INSERT INTO niche_adjacency (vertical, niche_a, niche_b, adjacency_type, rationale, source)
                    VALUES (%s, %s, %s, %s, %s, 'curated')
                    ON CONFLICT (vertical, niche_a, niche_b, adjacency_type) DO NOTHING""",
                (p["vertical"], p["a"], p["b"], p["type"], p["rationale"]),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
        for c in V4_COHORT_DEFS:
            cur.execute(
                """INSERT INTO cohort_definitions (cohort_code, vertical, exclusive_group, label, description)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (cohort_code) DO NOTHING""",
                (c["code"], c["vertical"], c["exclusive_group"], c["label"], c["desc"]),
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


# === v4 seed data (plan §5.1, §5.4, §5.10) ===

V4_PRIMARY_NICHE_GROUPS: list[dict[str, str]] = [
    # Crime — 5 primary niche groups matching the brief's named categories
    {"vertical": "crime", "label": "Bodycam / Police Incidents",
     "desc": "Raw bodycam footage, police incident recordings, dashcam, and first-responder video content"},
    {"vertical": "crime", "label": "Police Investigation Documentary",
     "desc": "Long-form documentary-style coverage of police investigations, detective work, and case breakdowns"},
    {"vertical": "crime", "label": "Interrogation / Criminal Psychology",
     "desc": "Interrogation footage analysis, criminal psychology breakdowns, suspect interview analysis"},
    {"vertical": "crime", "label": "Cold Case / Unsolved",
     "desc": "Cold case investigations, unsolved mysteries, long-dormant case re-examinations"},
    {"vertical": "crime", "label": "Digital Evidence / Internet Crime",
     "desc": "Digital evidence analysis, internet crime, cyber-investigation, social-media-based crime"},

    # Finance — 4 macro-groups matching the brief's named categories
    {"vertical": "finance", "label": "Core / Storytelling",
     "desc": "Financial storytelling, economic narratives, and accessible finance explainers"},
    {"vertical": "finance", "label": "Investing / Markets",
     "desc": "Stock market analysis, investing strategies, portfolio management, and market commentary"},
    {"vertical": "finance", "label": "Personal Finance",
     "desc": "Personal budgeting, saving strategies, debt management, and financial literacy"},
    {"vertical": "finance", "label": "Emerging / Audience-Specific",
     "desc": "Finance for Gen Z, finance for women, niche-audience financial content"},
]

V4_NICHE_ADJACENCY: list[dict[str, str]] = [
    # Crime sibling_overlap — the 5 named underrepresented niches as one cluster
    {"vertical": "crime", "a": "bodycam_footage", "b": "police_investigation_documentary",
     "type": "sibling_overlap",
     "rationale": "Same procedural subject — documentary framing over the same incidents bodycam captures raw"},
    {"vertical": "crime", "a": "bodycam_footage", "b": "interrogation_criminal_psychology",
     "type": "sibling_overlap",
     "rationale": "Interrogations often follow the incident a bodycam captured — related procedural timeline"},
    {"vertical": "crime", "a": "bodycam_footage", "b": "cold_case_unsolved",
     "type": "sibling_overlap",
     "rationale": "Cold-case channels frequently open episodes with original responding-officer footage"},
    {"vertical": "crime", "a": "bodycam_footage", "b": "digital_evidence_internet_crime",
     "type": "format_shared",
     "rationale": "Different subject matter but similar evidence-presentation format — worth checking, lower prior"},
    {"vertical": "crime", "a": "police_investigation_documentary", "b": "interrogation_criminal_psychology",
     "type": "sibling_overlap",
     "rationale": "Documentary investigations frequently include interrogation segments as key evidence"},
    {"vertical": "crime", "a": "police_investigation_documentary", "b": "cold_case_unsolved",
     "type": "sibling_overlap",
     "rationale": "Same documentary format applied to active vs. cold investigations — substantial format overlap"},
    {"vertical": "crime", "a": "interrogation_criminal_psychology", "b": "cold_case_unsolved",
     "type": "sibling_overlap",
     "rationale": "Criminal psychology analysis applies identically to current and reopened cases"},

    # Finance parent_child — macro-groups to their sub-groups
    {"vertical": "finance", "a": "personal_finance_budgeting", "b": "investing_stock_market",
     "type": "sibling_overlap",
     "rationale": "Personal finance audiences frequently cross into investing content — highest-traffic cross-niche path"},
    {"vertical": "finance", "a": "investing_stock_market", "b": "crypto_web3",
     "type": "sibling_overlap",
     "rationale": "Investing and crypto share an audience with high cross-over, especially among younger demographics"},
    {"vertical": "finance", "a": "personal_finance_budgeting", "b": "crypto_web3",
     "type": "format_shared",
     "rationale": "Different asset class but similar explainer/tutorial format — lower prior than the investing bridge"},

    # --- v4 expanded Finance adjacency (brief §2 missing niches) ---
    {"vertical": "finance", "a": "investing_stock_market", "b": "etf_passive_investing",
     "type": "parent_child",
     "rationale": "ETF investing is the most common entry point from general stock-market content"},
    {"vertical": "finance", "a": "investing_stock_market", "b": "dividend_investing",
     "type": "sibling_overlap",
     "rationale": "Dividend investors are a large subset of the stock-market audience"},
    {"vertical": "finance", "a": "investing_stock_market", "b": "value_investing",
     "type": "sibling_overlap",
     "rationale": "Value investing methodology extends naturally from stock analysis content"},
    {"vertical": "finance", "a": "investing_stock_market", "b": "options_trading",
     "type": "sibling_overlap",
     "rationale": "Options traders commonly produce stock-market content as their baseline"},
    {"vertical": "finance", "a": "investing_stock_market", "b": "market_analysis",
     "type": "sibling_overlap",
     "rationale": "Market analysis is a direct extension of stock-market content"},
    {"vertical": "finance", "a": "investing_stock_market", "b": "stock_market_stories",
     "type": "sibling_overlap",
     "rationale": "Stock-market narrative channels share an audience with pure analysis channels"},
    {"vertical": "finance", "a": "personal_finance_budgeting", "b": "financial_independence_fire",
     "type": "sibling_overlap",
     "rationale": "FIRE content is the most popular lens on personal finance for younger demographics"},
    {"vertical": "finance", "a": "personal_finance_budgeting", "b": "debt_credit",
     "type": "sibling_overlap",
     "rationale": "Debt/credit management is the complement of budgeting/saving content"},
    {"vertical": "finance", "a": "personal_finance_budgeting", "b": "wealth_building",
     "type": "sibling_overlap",
     "rationale": "Budgeting is the practical step behind long-term wealth building strategies"},
    {"vertical": "finance", "a": "personal_finance_budgeting", "b": "retirement",
     "type": "sibling_overlap",
     "rationale": "Retirement planning is the endgame of personal finance — shared audience"},
    {"vertical": "finance", "a": "personal_finance_budgeting", "b": "tax_education",
     "type": "sibling_overlap",
     "rationale": "Tax strategy is the practical complement to personal finance content"},
    {"vertical": "finance", "a": "crypto_web3", "b": "ai_finance",
     "type": "audience_shared",
     "rationale": "Crypto and AI audiences share a heavy tech-early-adopter overlap"},
    {"vertical": "finance", "a": "finance_documentary", "b": "economic_storytelling",
     "type": "sibling_overlap",
     "rationale": "Documentary and storytelling formats share the same production style and audience"},
    {"vertical": "finance", "a": "finance_documentary", "b": "financial_history",
     "type": "sibling_overlap",
     "rationale": "Financial history is a natural documentary subject"},
    {"vertical": "finance", "a": "finance_documentary", "b": "company_breakdowns",
     "type": "sibling_overlap",
     "rationale": "Company deep-dives are one of the most common documentary-style finance formats"},
    {"vertical": "finance", "a": "finance_documentary", "b": "financial_scams_fraud",
     "type": "sibling_overlap",
     "rationale": "Scam/fraud documentaries share the same investigative-narrative format"},
    {"vertical": "finance", "a": "personal_finance_psychology", "b": "behavioral_finance",
     "type": "sibling_overlap",
     "rationale": "Psychology and behavioral finance are two lenses on the same subject"},
    {"vertical": "finance", "a": "geopolitics_finance", "b": "macro_investing",
     "type": "sibling_overlap",
     "rationale": "Geopolitical analysis feeds directly into macro investing decisions"},
    {"vertical": "finance", "a": "finance_gen_z", "b": "finance_young_adults",
     "type": "sibling_overlap",
     "rationale": "Gen Z and young-adult audiences overlap heavily in content style and platform"},
    {"vertical": "finance", "a": "finance_gen_z", "b": "creator_economy_finance",
     "type": "audience_shared",
     "rationale": "Gen Z's financial curiosity centers on creator-economy income models"},
]

V4_COHORT_DEFS: list[dict[str, str]] = [
    # Crime — 4 mutually exclusive cohorts in one exclusive_group
    {"code": "market_benchmark", "vertical": "crime", "exclusive_group": "crime_lifecycle",
     "label": "Market Benchmark", "desc": "Established channels with sustained high performance — the leader set"},
    {"code": "growth_competitor", "vertical": "crime", "exclusive_group": "crime_lifecycle",
     "label": "Growth Competitor", "desc": "Channels showing rapid growth trajectory, potentially contesting benchmark positions"},
    {"code": "new_entrant_breakout", "vertical": "crime", "exclusive_group": "crime_lifecycle",
     "label": "New Entrant Breakout", "desc": "Channels created 2024-2026 showing breakout-level early performance"},
    {"code": "underperformer", "vertical": "crime", "exclusive_group": "crime_lifecycle",
     "label": "Underperformer", "desc": "Channels with meaningful video history but subpar performance relative to niche peers"},

    # Finance — independent flag (is_new_channel) + mutually exclusive winner/loser pair
    {"code": "is_new_channel", "vertical": "finance", "exclusive_group": None,
     "label": "New Channel", "desc": "Channel created 2024-2026 — an independent flag, not mutually exclusive with winner/loser"},
    {"code": "new_winner", "vertical": "finance", "exclusive_group": "finance_outcome",
     "label": "New Winner", "desc": "New channel (2024-2026) demonstrating breakout performance vs. age-controlled peers"},
    {"code": "new_loser", "vertical": "finance", "exclusive_group": "finance_outcome",
     "label": "New Loser", "desc": "New channel (2024-2026) underperforming vs. age-controlled peers — the comparison set"},
]