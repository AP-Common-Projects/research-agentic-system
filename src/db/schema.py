from __future__ import annotations


import structlog

logger = structlog.get_logger(__name__)
DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    subscriber_count BIGINT DEFAULT 0,
    description TEXT DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    discovery_method TEXT NOT NULL,
    extra JSONB DEFAULT '{}'::jsonb,
    channel_embedding vector(1536)
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    view_count BIGINT DEFAULT 0,
    like_count BIGINT DEFAULT 0,
    comment_count BIGINT DEFAULT 0,
    published_at TIMESTAMPTZ,
    outlier_score DOUBLE PRECISION DEFAULT 0,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extra JSONB DEFAULT '{}'::jsonb
);

-- Membership: which entities belong to which branch of which run.
--
-- `channels` and `videos` are deliberately NOT run-scoped — a channel is a
-- shared entity that can legitimately appear in both a Finance run and a Legal
-- one, so stamping a run_id on the row would either duplicate it or lose the
-- overlap. This is the many-to-many table that carries that relationship, and
-- it is what makes a per-run export possible without wiping the store between
-- runs.
--
-- run_id is part of the key because tree node ids are only unique WITHIN a
-- run: every run has a "root".
CREATE TABLE IF NOT EXISTS category_tags (
    id SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('channel', 'video')),
    entity_id TEXT NOT NULL,
    tree_node_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    tagged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_type, entity_id, tree_node_id, run_id)
);

-- Graph traversal is keyed on channel *refs* (handle/URL): featured_channels
-- edges name their target by URL and handle but carry no UC id, and an id only
-- exists once that channel has itself been fetched. So target_channel_ref is
-- the always-present identity and target_channel_id is filled in later, if and
-- when the walk resolves it. Uniqueness therefore keys on the ref.
CREATE TABLE IF NOT EXISTS discovery_edges (
    id SERIAL PRIMARY KEY,
    source_channel_id TEXT NOT NULL,
    target_channel_id TEXT,
    target_channel_ref TEXT NOT NULL,
    edge_type TEXT NOT NULL CHECK (edge_type IN (
        'featured_channel', 'recommendation', 'comment_author',
        'playlist', 'collaboration', 'comment_mention'
    )),
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id TEXT NOT NULL,
    UNIQUE (source_channel_id, target_channel_ref, edge_type)
);

CREATE TABLE IF NOT EXISTS analysis_results (
    video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    transcript TEXT,
    scene_cut_count INTEGER DEFAULT 0,
    cuts_per_minute DOUBLE PRECISION DEFAULT 0,
    multimodal_summary TEXT,
    analyzed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_discovery_edges_source_target ON discovery_edges(source_channel_id, target_channel_id);
CREATE INDEX IF NOT EXISTS idx_category_tags_entity_tree ON category_tags(entity_id, tree_node_id);
CREATE INDEX IF NOT EXISTS idx_category_tags_tree_node ON category_tags(tree_node_id);
"""


# Applied after DDL, in order, every time. `CREATE TABLE IF NOT EXISTS` does
# nothing to a table that already exists, so a database created before the
# discovery_edges reshape would keep the old NOT NULL target_channel_id, the
# old UNIQUE key and the old CHECK — and every featured_channel edge would be
# rejected at insert time. Each statement here is written to be safe to run on
# an already-migrated database.
MIGRATIONS: list[str] = [
    "ALTER TABLE discovery_edges ADD COLUMN IF NOT EXISTS target_channel_ref TEXT",
    "ALTER TABLE discovery_edges ALTER COLUMN target_channel_id DROP NOT NULL",
    # Backfill refs for any rows written under the old id-only shape, so the
    # NOT NULL and UNIQUE below can be applied without dropping data.
    """UPDATE discovery_edges
          SET target_channel_ref = 'https://www.youtube.com/channel/' || target_channel_id
        WHERE target_channel_ref IS NULL AND target_channel_id IS NOT NULL""",
    # Only rows that carry neither identifier. Guarding on target_channel_id
    # matters because each statement commits independently: if the backfill
    # above failed and rolled back, an unguarded `WHERE target_channel_ref IS
    # NULL` would delete every pre-existing edge in the table.
    """DELETE FROM discovery_edges
        WHERE target_channel_ref IS NULL AND target_channel_id IS NULL""",
    "ALTER TABLE discovery_edges ALTER COLUMN target_channel_ref SET NOT NULL",
    "ALTER TABLE discovery_edges DROP CONSTRAINT IF EXISTS discovery_edges_edge_type_check",
    """ALTER TABLE discovery_edges ADD CONSTRAINT discovery_edges_edge_type_check
       CHECK (edge_type IN ('featured_channel', 'recommendation', 'comment_author',
                            'playlist', 'collaboration', 'comment_mention'))""",
    """ALTER TABLE discovery_edges
       DROP CONSTRAINT IF EXISTS discovery_edges_source_channel_id_target_channel_id_edge_ty_key""",
    """CREATE UNIQUE INDEX IF NOT EXISTS discovery_edges_source_ref_type_key
       ON discovery_edges (source_channel_id, target_channel_ref, edge_type)""",
    """CREATE INDEX IF NOT EXISTS idx_discovery_edges_run
       ON discovery_edges (run_id)""",
    # category_tags gains run_id: tree node ids repeat across runs (every run
    # has a "root"), so without it a per-run export cannot be scoped.
    "ALTER TABLE category_tags ADD COLUMN IF NOT EXISTS run_id TEXT NOT NULL DEFAULT ''",
    """ALTER TABLE category_tags
       DROP CONSTRAINT IF EXISTS category_tags_entity_type_entity_id_tree_node_id_key""",
    """CREATE UNIQUE INDEX IF NOT EXISTS category_tags_entity_node_run_key
       ON category_tags (entity_type, entity_id, tree_node_id, run_id)""",
    """CREATE INDEX IF NOT EXISTS idx_category_tags_run
       ON category_tags (run_id)""",
]


def create_schema(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    apply_migrations(conn)


def apply_migrations(conn) -> None:
    """Run each migration independently so one failure can't strand the rest.

    Committing per statement rather than as one transaction matters here: these
    run on every startup, and a statement that is already satisfied should not
    roll back the ones after it.
    """
    for statement in MIGRATIONS:
        cur = conn.cursor()
        try:
            cur.execute(statement)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            # Log, never swallow silently. Most failures here are the intended
            # "already applied" no-op, but a genuinely failed
            # CREATE UNIQUE INDEX leaves a database where every persist_edge
            # ON CONFLICT raises "no unique or exclusion constraint matching" —
            # and persist_edges swallows per-edge failures too, so the visible
            # result is an empty discovery_edges table and a green run.
            # discovery_edges is the sole evidence for this project's core
            # claim, so that failure has to be findable.
            logger.warning(
                "schema_migration_skipped",
                statement=" ".join(statement.split())[:120],
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
        finally:
            cur.close()


def ensure_schema() -> None:
    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        create_schema(conn)
    finally:
        put_connection(conn)


def drop_schema(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute("DROP TABLE IF EXISTS analysis_results CASCADE")
        cur.execute("DROP TABLE IF EXISTS discovery_edges CASCADE")
        cur.execute("DROP TABLE IF EXISTS category_tags CASCADE")
        cur.execute("DROP TABLE IF EXISTS videos CASCADE")
        cur.execute("DROP TABLE IF EXISTS channels CASCADE")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()