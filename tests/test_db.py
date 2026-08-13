from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import src.db.connection as db_conn
from src.db.connection import (
    close_pools,
    get_connection,
    get_pool,
    put_connection,
)
from src.db.schema import create_schema, drop_schema, ensure_schema, MIGRATIONS


@pytest.fixture(autouse=True)
def _reset_pools():
    close_pools()
    yield
    close_pools()


class TestConnectionPool:
    def test_get_pool_creates_singleton(self, mocker):
        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mocker.patch("src.db.connection.ConnectionPool", return_value=mock_pool)

        p1 = get_pool()
        p2 = get_pool()

        assert p1 is p2
        assert p1 is mock_pool

    def test_get_pool_passes_config(self, mocker):
        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mock_constructor = mocker.patch(
            "src.db.connection.ConnectionPool", return_value=mock_pool
        )

        get_pool()

        mock_constructor.assert_called_once()
        _, kwargs = mock_constructor.call_args
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 10
        assert kwargs["timeout"] == 8.0

    def test_get_pool_construction_never_blocks(self, mocker):
        """Regression: `open=True` makes the ConnectionPool constructor itself
        call open(wait=True) and block up to `timeout` — and if that raises,
        `_pool` is never assigned, so the *next* call reconstructs from
        scratch and blocks again. Every request against an unreachable store
        paid that wait independently, forever. Construction must be
        non-blocking (`open=False` + an explicit `open(wait=False)`), so the
        pool object is memoized on the first call regardless of whether
        Postgres is reachable yet."""
        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mock_constructor = mocker.patch(
            "src.db.connection.ConnectionPool", return_value=mock_pool
        )

        get_pool()

        _, kwargs = mock_constructor.call_args
        assert kwargs["open"] is False
        mock_pool.open.assert_called_once_with(wait=False)

    def test_get_pool_reused_even_after_a_failed_open(self, mocker):
        """The pool object must stay memoized so its background reconnect
        workers keep retrying across requests, instead of every request
        constructing (and blocking on) a brand-new pool."""
        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mocker.patch("src.db.connection.ConnectionPool", return_value=mock_pool)

        p1 = get_pool()
        mock_pool.getconn.side_effect = TimeoutError("store unreachable")
        with pytest.raises(TimeoutError):
            get_connection()

        p2 = get_pool()
        assert p1 is p2

    def test_get_connection_delegates_to_pool(self, mocker):
        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mock_conn = mocker.MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mocker.patch("src.db.connection.ConnectionPool", return_value=mock_pool)

        conn = get_connection()

        assert conn is mock_conn
        mock_pool.getconn.assert_called_once()

    def test_put_connection_returns_to_pool(self, mocker):
        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mock_conn = mocker.MagicMock()
        mocker.patch("src.db.connection.ConnectionPool", return_value=mock_pool)

        put_connection(mock_conn)

        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_get_connection_delegates_sync(self, mocker):
        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mock_conn = mocker.MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mocker.patch("src.db.connection.ConnectionPool", return_value=mock_pool)

        conn = get_connection()

        assert conn is mock_conn
        mock_pool.getconn.assert_called_once()

    def test_close_pools_cleans_up(self, mocker):
        mock_sync = mocker.MagicMock(spec=ConnectionPool)
        mocker.patch("src.db.connection.ConnectionPool", return_value=mock_sync)

        get_pool()
        assert db_conn._pool is not None

        close_pools()

        mock_sync.close.assert_called_once()
        assert db_conn._pool is None


class TestCreateSchema:
    def test_executes_ddl_and_commits(self, mocker):
        mock_cursor = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        create_schema(mock_conn)

        # DDL first, then the idempotent migrations — CREATE TABLE IF NOT
        # EXISTS is a no-op on an existing database, so the ALTERs are the
        # only thing that reshapes discovery_edges on an already-created store.
        executed_sql = mock_cursor.execute.call_args_list[0][0][0]
        assert "CREATE TABLE IF NOT EXISTS channels" in executed_sql
        assert "CREATE TABLE IF NOT EXISTS videos" in executed_sql
        assert "CREATE TABLE IF NOT EXISTS category_tags" in executed_sql
        assert "CREATE TABLE IF NOT EXISTS discovery_edges" in executed_sql
        assert "CREATE TABLE IF NOT EXISTS analysis_results" in executed_sql
        assert "CREATE EXTENSION IF NOT EXISTS vector" in executed_sql
        mock_conn.commit.assert_called()
        mock_cursor.close.assert_called()

    def test_runs_migrations_after_ddl(self, mocker):
        mock_cursor = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        create_schema(mock_conn)

        statements = [c[0][0] for c in mock_cursor.execute.call_args_list]
        assert len(statements) == 1 + len(MIGRATIONS)
        joined = "\n".join(statements[1:])
        assert "ADD COLUMN IF NOT EXISTS target_channel_ref" in joined
        assert "target_channel_id DROP NOT NULL" in joined
        assert "featured_channel" in joined

    def test_one_failed_migration_does_not_strand_the_rest(self, mocker):
        """Migrations run on every startup, so an already-satisfied statement
        must not prevent the ones after it from being attempted."""
        mock_cursor = mocker.MagicMock()
        mock_cursor.execute.side_effect = [
            None,  # DDL
            psycopg.errors.DuplicateColumn("already there"),
        ] + [None] * len(MIGRATIONS)
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        create_schema(mock_conn)

        assert mock_cursor.execute.call_count == 1 + len(MIGRATIONS)
        mock_conn.rollback.assert_called_once()

    def test_rollback_on_error(self, mocker):
        mock_cursor = mocker.MagicMock()
        mock_cursor.execute.side_effect = psycopg.OperationalError("boom")
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with pytest.raises(psycopg.OperationalError, match="boom"):
            create_schema(mock_conn)

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()
        mock_cursor.close.assert_called_once()

    def test_idempotent_two_calls(self, mocker):
        mock_cursor = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        create_schema(mock_conn)
        create_schema(mock_conn)

        # One commit for the DDL plus one per migration, per call. The point
        # is that the second call is safe to make, not that it is silent.
        assert mock_conn.commit.call_count == 2 * (1 + len(MIGRATIONS))

    def test_all_tables_present_in_ddl(self):
        from src.db.schema import DDL

        assert "CREATE TABLE IF NOT EXISTS channels" in DDL
        assert "CREATE TABLE IF NOT EXISTS videos" in DDL
        assert "CREATE TABLE IF NOT EXISTS category_tags" in DDL
        assert "CREATE TABLE IF NOT EXISTS discovery_edges" in DDL
        assert "CREATE TABLE IF NOT EXISTS analysis_results" in DDL

    def test_indexes_present_in_ddl(self):
        from src.db.schema import DDL

        assert "idx_videos_channel_id" in DDL
        assert "idx_discovery_edges_source_target" in DDL
        assert "idx_category_tags_entity_tree" in DDL
        assert "idx_category_tags_tree_node" in DDL

    def test_channel_embedding_column_present(self):
        from src.db.schema import DDL

        assert "channel_embedding vector(1536)" in DDL

    def test_fk_constraints_in_ddl(self):
        from src.db.schema import DDL

        assert "REFERENCES channels(channel_id) ON DELETE CASCADE" in DDL
        assert "REFERENCES videos(video_id) ON DELETE CASCADE" in DDL


class TestForeignKeyEnforcement:
    def test_video_insert_missing_channel_raises(self, mocker):
        fk_error = psycopg.errors.ForeignKeyViolation(
            'insert or update on table "videos" violates foreign key constraint'
        )
        mock_cursor = mocker.MagicMock()
        mock_cursor.execute.side_effect = fk_error
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            create_schema(mock_conn)

        mock_conn.rollback.assert_called_once()

    def test_analysis_insert_missing_video_raises(self, mocker):
        fk_error = psycopg.errors.ForeignKeyViolation(
            'insert or update on table "analysis_results" violates foreign key constraint'
        )
        mock_cursor = mocker.MagicMock()
        mock_cursor.execute.side_effect = fk_error
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            create_schema(mock_conn)

        mock_conn.rollback.assert_called_once()


class TestEnsureSchema:
    def test_gets_conn_calls_create_schema_puts_back(self, mocker):
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mocker.MagicMock()
        mocker.patch("src.db.connection.get_connection", return_value=mock_conn)
        mock_put = mocker.patch("src.db.connection.put_connection")

        ensure_schema()

        mock_put.assert_called_once_with(mock_conn)

    def test_puts_back_on_error(self, mocker):
        mock_conn = mocker.MagicMock()
        mock_cursor = mocker.MagicMock()
        mock_cursor.execute.side_effect = psycopg.OperationalError("oops")
        mock_conn.cursor.return_value = mock_cursor
        mocker.patch("src.db.connection.get_connection", return_value=mock_conn)
        mock_put = mocker.patch("src.db.connection.put_connection")

        with pytest.raises(psycopg.OperationalError):
            ensure_schema()

        mock_put.assert_called_once_with(mock_conn)


class TestDropSchema:
    def test_drops_tables_in_order(self, mocker):
        mock_cursor = mocker.MagicMock()
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        drop_schema(mock_conn)

        assert mock_cursor.execute.call_count >= 5
        calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
        assert any("analysis_results" in c for c in calls)
        assert any("discovery_edges" in c for c in calls)
        assert any("category_tags" in c for c in calls)
        assert any("videos" in c for c in calls)
        assert any("channels" in c for c in calls)
        mock_conn.commit.assert_called_once()

    def test_rollback_on_error(self, mocker):
        mock_cursor = mocker.MagicMock()
        mock_cursor.execute.side_effect = [None, psycopg.OperationalError("oops")]
        mock_conn = mocker.MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with pytest.raises(psycopg.OperationalError):
            drop_schema(mock_conn)

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()


class TestCheckpointer:
    @pytest.fixture(autouse=True)
    def _reset_setup_memo(self, mocker):
        """Table creation is memoized per process; unset it so each test sees
        the first-call behaviour."""
        import src.db.checkpointer as cp

        mocker.patch.object(cp, "_tables_ready", False)

    def test_sync_saver_is_built_on_the_project_pool(self, mocker):
        import src.db.checkpointer as cp

        mock_pool = mocker.MagicMock(spec=ConnectionPool)
        mocker.patch.object(cp, "get_pool", return_value=mock_pool)
        mocker.patch.object(cp, "_ensure_checkpoint_tables")
        mock_saver_cls = mocker.patch.object(cp, "PostgresSaver")

        result = cp.get_checkpointer()

        mock_saver_cls.assert_called_once_with(mock_pool)
        assert result is mock_saver_cls.return_value

    def test_setup_runs_on_a_dedicated_autocommit_connection(self, mocker):
        """PostgresSaver.setup() issues CREATE INDEX CONCURRENTLY, which
        Postgres refuses inside a transaction block. Pool connections are not
        autocommit — deliberately, since the store depends on explicit
        commit/rollback — so setup gets its own connection."""
        import src.db.checkpointer as cp

        mock_connect = mocker.patch.object(cp.psycopg, "connect")
        mocker.patch.object(cp, "PostgresSaver")

        cp._ensure_checkpoint_tables()

        assert mock_connect.call_args.kwargs["autocommit"] is True

    def test_table_setup_is_memoized(self, mocker):
        import src.db.checkpointer as cp

        mock_connect = mocker.patch.object(cp.psycopg, "connect")
        mocker.patch.object(cp, "PostgresSaver")

        cp._ensure_checkpoint_tables()
        cp._ensure_checkpoint_tables()

        assert mock_connect.call_count == 1

    @pytest.mark.asyncio
    async def test_pipeline_gets_the_async_saver(self, mocker):
        """run_pipeline drives the graph with ainvoke, so LangGraph calls
        aget_tuple/aput. PostgresSaver inherits those as
        `raise NotImplementedError` — a run with the sync saver dies on the
        first superstep, before any node executes."""
        import src.db.checkpointer as cp

        mock_pool = mocker.MagicMock()
        mocker.patch.object(cp, "get_async_pool", new=mocker.AsyncMock(return_value=mock_pool))
        mocker.patch.object(cp, "_ensure_checkpoint_tables")
        mock_async_cls = mocker.patch.object(cp, "AsyncPostgresSaver")

        result = await cp.get_async_checkpointer()

        mock_async_cls.assert_called_once_with(mock_pool)
        assert result is mock_async_cls.return_value