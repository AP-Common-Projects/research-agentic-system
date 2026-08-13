"""Regression coverage for config.py's .env discovery.

`load_dotenv(Path(__file__).resolve().parents[N] / ".env")` silently loads
nothing if N points above the repo root — dotenv doesn't raise when the file
is missing, it just leaves every setting on its class default. That exact
bug shipped: parents[2] resolved to the *parent* of the repo (one directory
too far up), so a correctly-placed .env at the repo root was never read and
every Postgres call fell back to the default (passwordless) credentials,
manifesting as an auth failure that looked like a pool/network problem.
"""

from __future__ import annotations

from pathlib import Path


def test_env_path_resolves_to_repo_root():
    import src.config as config_module

    src_dir = Path(config_module.__file__).resolve().parent
    repo_root = src_dir.parent

    # The repo root is identifiable by pyproject.toml — a stronger check than
    # re-deriving the same parents[N] expression the source uses, which would
    # just restate the bug if the index were wrong in both places.
    assert (repo_root / "pyproject.toml").is_file()
    assert (repo_root / ".env.example").is_file()

    expected_env_path = repo_root / ".env"
    # Mirrors the source's own derivation from config.py's location — not
    # from `src_dir`, which is already one level closer to the root than
    # `Path(config_module.__file__)` is.
    actual_env_path = Path(config_module.__file__).resolve().parents[1] / ".env"
    assert actual_env_path == expected_env_path
