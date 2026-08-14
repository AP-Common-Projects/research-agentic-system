# Architecture Review — six-point check against v1 code

Run by `architecture-reviewer` (read-only) against the built v1 code, 13 Aug 2026.
This is the honest result — real bugs were found and fixed, not rubber-stamped.

## Six-point result

1. **State & reducers — FIXED.** The tree reducer was last-write-wins per node-id,
   so `keyword_search` and `graph_walk` — which both write `tree={active_node_id: ...}`
   in the same fan-out step with disjoint fields — silently dropped one branch's
   updates. This is the missing-reducer bug class. Fixed with a per-field deep merge
   (`_merge_tree_dict` in `src/state.py`) plus delta-only tree writes from each branch.
   All other accumulating fields verified to have correct reducers (dedup append,
   set union, float accumulate, `a+b`).

2. **Idempotency — FIXED.** Upserts are keyed on natural keys (`channel_id`,
   `video_id`, `(source,target,edge_type)`). BUT `hydrate_metadata` passed
   `first_seen_at=""`, which broke the `TIMESTAMPTZ NOT NULL` insert — every channel
   write failed silently under `except: pass`, so the store was never populated
   (HIGH SEVERITY). Fixed with `COALESCE(NULLIF(...,''), NOW())` in `dedup.py` and
   real ISO timestamps in `hydrate_metadata`.

3. **Frontier discipline — PASS (was FAIL by proxy of #1).** `graph_walk` expands
   only `unexpanded - expanded` (never re-touches expanded seeds); novelty computed
   over the current round's frontier only. `keyword_search`'s `queries_run` prevents
   re-submitting the same query set. The re-scan regression fixture in `tests/test_tools.py`
   passes.

4. **Fan-out/fan-in — PASS.** Join point defined (both branches → `hydrate_metadata`),
   per-branch failure defined via `_guarded` (record error + mark done + continue).

5. **Checkpointing — FIXED.** `PostgresSaver` configured, `thread_id` one-per-run.
   BUT resume overwrote checkpointed state with a fresh initial state and regenerated
   `run_id`. Fixed: `run_pipeline(resume=True)` loads + migrates the checkpoint via
   `get_state` before continuing. `migrate_state` now runs on load.

6. **Schema evolution — FIXED (partial by design).** `TreeNode.schema_version` present
   and inherited by proposed nodes; `migrate_state` covers v0→v3 (`hydrated_channel_ids`
   added as v3). Applied on resume. Dynamic taxonomy growth (compact_branch new-node
   proposal) is the only runtime schema change and is covered.

## Additional findings (all fixed unless noted)

- **Novelty conflation** — both branches appended one observation/round to a global
  list, so `window=3` measured ~1.5 rounds. Fixed: per-track per-node novelty history
  (`_kw_novelty_history` / `_gw_novelty_history`) + exhaustion flags.
- **Infinite loop on empty node** — a node with no keywords and no seeds returned
  done-flags without novelty, never saturating. Fixed: `check_saturation` marks a node
  saturated when both tracks report exhaustion.
- **`score_signals` stub** — returned `{"next_action": "continue"}` without computing
  anything. Fixed: computes engagement rate / cadence / velocity and persists to
  channel `extra` JSONB.
- **`hydrate_metadata` re-fetched the entire cumulative set every round** — quota waste.
  Fixed: hydrates only the round's delta via `hydrated_channel_ids`.
- **`set[str]` in state** — verified LangGraph's `JsonPlusSerializer` round-trips sets
  correctly through `PostgresSaver`; no change needed.
- **Budget exhaustion compacts only the active node** — left as-is; matches the plan
  §0.1 item 4 ("compact whatever's in flight, then synthesize"). Other saturated nodes'
  channels remain in the store for synthesis grading.

## Verdict

No remaining known architecture bugs. The two bug classes the plan named (§0.2) —
missing reducers and cumulative re-scan — both have regression tests that fail on the
old behavior and pass on the fixed code.

## v2 adaptive-depth follow-up (14 Aug 2026)

gentic-architecture-review six-point check applied to the cluster_branch
change (ADR-0007), against actual code rather than the design:

1. State & reducers — PASS. The three new TreeNode fields live inside the
   tree dict and go through the existing deep-merge reducer _merge_tree_dict.
   cluster_branch is a single sequential writer (not a fan-out participant).
   ranch_lineage_spend uses a new additive-per-key reducer — keyword_search
   and graph_walk both report deltas against the same lineage root in the same
   superstep, so they must SUM, not max.
2. Idempotency — PASS, CONDITIONAL ON THE SEED. cluster_branch recomputes
   deterministically on every call, but only because detect_communities
   passes seed=cfg.cluster_seed (42). Unseeded, a resumed run recomputes
   different clusters than the original. Verified by test
   	est_seed_reproducibility.
3. Frontier discipline — NOT APPLICABLE. Operates once after saturation on a
   fixed resolved set; does not expand a frontier.
4. Fan-out/fan-in — NOT APPLICABLE. Single predecessor (check_saturation),
   single successor (compact_branch).
5. Checkpointing — PASS. Returns through the standard node-return-dict path,
   picked up by PostgresSaver with no special-casing.
6. Schema evolution — FIXED. v5 -> v6 migrate_state backfills
   split_method="llm_seed", cluster_member_channel_ids=[],
   cluster_distinctness_score=None, and derives lineage_root_id from
   depth-1 ancestors. Verified by 	est_migrates_v5_tree_nodes_to_v6.

Two clean passes, two not-applicable, one conditional pass with a specific
named risk (the seed), one required-and-applied migration fix.
