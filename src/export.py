"""Export a run's research output as files a human can be handed.

The store answers queries; it does not produce a deliverable. This turns one
run into a directory that a Content Creation Team can open and that Claude can
re-read later to write a hand-off report.

Run-scoped through `category_tags`, not through the store's entity tables.
`channels` and `videos` are shared — a channel can belong to a Finance run and
a Legal one — so the run's slice comes from the membership table plus the
run's own checkpoint, and two categories can coexist in one database without
either export contaminating the other.

Both CSV and JSON, deliberately. CSV opens in Excel for the team; JSON keeps
the structure for whatever reads it next. `manifest.json` carries provenance —
profile, spend, and *why the run stopped* — because every run so far has
terminated on a governor with novelty still high, and a report that does not
say so reads as more complete than it is.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.config import get_config

logger = structlog.get_logger(__name__)


def export_dir(run_id: str) -> Path:
    base = Path(get_config().harness.export_dir) / run_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fetch(sql: str, params: tuple) -> list[dict[str, Any]]:
    from src.db.connection import get_connection, put_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()
    finally:
        put_connection(conn)


def fetch_run_channels(run_id: str) -> list[dict[str, Any]]:
    """Channels tagged to this run, with their computed signals flattened."""
    return _fetch(
        """
        SELECT DISTINCT c.channel_id, c.title, c.subscriber_count,
               c.discovery_method, c.first_seen_at, c.description,
               (c.extra->>'engagement_rate')::float AS engagement_rate,
               (c.extra->>'cadence')::float        AS cadence,
               (c.extra->>'velocity')::float       AS velocity
        FROM channels c
        JOIN category_tags t
          ON t.entity_id = c.channel_id AND t.entity_type = 'channel'
        WHERE t.run_id = %s
        ORDER BY c.subscriber_count DESC NULLS LAST
        """,
        (run_id,),
    )


def fetch_run_videos(run_id: str, limit: int) -> list[dict[str, Any]]:
    """This run's videos, highest outlier score first.

    Ordered by outlier score rather than views because the score is the point:
    a 200k-view video on a 5M-subscriber channel is unremarkable, while the
    same on a 20k channel is the signal the team is looking for.
    """
    return _fetch(
        """
        SELECT DISTINCT v.video_id, v.channel_id, c.title AS channel_title,
               v.title, v.view_count, v.like_count, v.comment_count,
               v.published_at, v.outlier_score
        FROM videos v
        JOIN category_tags t
          ON t.entity_id = v.video_id AND t.entity_type = 'video'
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE t.run_id = %s
        ORDER BY v.outlier_score DESC NULLS LAST
        LIMIT %s
        """,
        (run_id, limit),
    )


def fetch_run_edges(run_id: str) -> list[dict[str, Any]]:
    return _fetch(
        """
        SELECT source_channel_id, target_channel_id, target_channel_ref,
               edge_type, discovered_at
        FROM discovery_edges
        WHERE run_id = %s
        ORDER BY discovered_at
        """,
        (run_id,),
    )


def fetch_run_tree_counts(run_id: str) -> dict[str, dict[str, int]]:
    """Per-node coverage: how many channels/videos this run tagged under
    each tree node, from category_tags — the same membership table the rest
    of the export reads its run-scoped slice from."""
    rows = _fetch(
        """
        SELECT tree_node_id, entity_type, COUNT(DISTINCT entity_id) AS n
        FROM category_tags
        WHERE run_id = %s
        GROUP BY tree_node_id, entity_type
        """,
        (run_id,),
    )
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        node_counts = counts.setdefault(row["tree_node_id"], {"channels": 0, "videos": 0})
        if row["entity_type"] == "channel":
            node_counts["channels"] = row["n"]
        elif row["entity_type"] == "video":
            node_counts["videos"] = row["n"]
    return counts


_STATUS_LABELS = {
    "pending": "Pending",
    "active": "Active",
    "saturated": "Saturated (awaiting compaction)",
    "compacted": "Compacted",
}


def build_taxonomy_payload(
    tree: dict[str, dict[str, Any]],
    counts: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Every tree node, structured for a top-down decision-tree diagram.

    Topology comes from each node's own parent_id, never from the parent's
    children_ids list — that field is written at split time but not kept in
    sync afterwards (compacted Finance nodes all carry children_ids: []
    despite having a populated parent_id pointing at them), so parent_id is
    the only field that's reliably correct to derive structure from.

    Every node is included — a taxonomy tree runs to tens of nodes, not the
    thousands a discovery graph can reach, so there is no trim policy here.
    """
    counts = counts or {}
    nodes: list[dict[str, Any]] = []
    ids = set(tree.keys())
    for node_id, n in tree.items():
        cov = counts.get(node_id, {})
        nodes.append({
            "id": node_id,
            "label": n.get("label") or node_id,
            "depth": n.get("depth", 0),
            "parent_id": n.get("parent_id"),
            "status": n.get("status", "pending"),
            "split_method": n.get("split_method", "llm_seed"),
            "distinctness_score": n.get("cluster_distinctness_score"),
            "saturation_reason": n.get("saturation_reason"),
            "keywords": n.get("keywords", [])[:6],
            "seed_count": len(n.get("seed_channel_ids", [])),
            "channel_count": cov.get("channels", len(n.get("seed_channel_ids", []))),
            "video_count": cov.get("videos", 0),
            "compaction_summary": n.get("compaction_summary") or "",
        })

    # A node whose parent_id doesn't resolve within this tree (missing/None)
    # is a root for layout purposes — normally just "root" itself, but a
    # malformed or partial tree should still render rather than crash.
    for n in nodes:
        if n["parent_id"] not in ids:
            n["parent_id"] = None

    children: dict[str | None, list[str]] = {}
    for n in nodes:
        children.setdefault(n["parent_id"], []).append(n["id"])

    status_counts: dict[str, int] = {}
    for n in nodes:
        status_counts[n["status"]] = status_counts.get(n["status"], 0) + 1

    return {
        "nodes": nodes,
        "roots": children.get(None, []),
        "children": children,
        "status_counts": status_counts,
    }


_CATEGORY_LABELS = {
    "keyword": "Keyword only",
    "graph_walk": "Graph walk only",
    "both": "Both tracks",
    "unresolved": "Unresolved (frontier)",
}


def _ref_label(ref: str) -> str:
    """A human-readable label for a channel we only know by ref.

    ".../@handle" -> "@handle" (what a person actually recognises); the
    UC-id form has no readable content, so it's shown short-and-truncated
    rather than as a 24-character opaque string.
    """
    ref = (ref or "").rstrip("/")
    tail = ref.rsplit("/", 1)[-1] if ref else ""
    if tail.startswith("@"):
        return tail
    if tail.startswith("UC") and len(tail) > 14:
        return tail[:10] + "…"
    return tail or "(unknown)"


def _category(discovery_method: str | None) -> str:
    """Bucket a channel's attribution into one of the graph's four series.

    Only three are ever assigned a categorical hue (keyword / graph_walk /
    both) — a node-link layout is an all-pairs context (any two categories
    can end up adjacent anywhere on screen), and the reference palette's
    all-pairs floor only clears for its first three slots. Everything else
    — genuinely unattributed channels, and frontier refs never fetched at
    all — folds into one neutral "unresolved" bucket rather than claiming a
    fourth hue the palette doesn't clear for this chart form.
    """
    return discovery_method if discovery_method in ("keyword", "graph_walk", "both") else "unresolved"


def build_graph_payload(
    channels: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    max_nodes: int = 0,
) -> dict[str, Any]:
    """Nodes + edges for the interactive graph, keyed consistently.

    Every hydrated channel becomes a node. Every edge's target that never
    resolved to a fetched channel — a frontier ref the walk saw but did not
    expand — ALSO becomes a node, keyed by its ref rather than an id it
    doesn't have. Without this the graph would only show the ~28% of the
    discovered network that got hydrated and silently drop the rest, when
    the unhydrated majority is itself the finding: it's the frontier the
    record budget ran out before reaching.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for ch in channels:
        cid = ch.get("channel_id", "")
        if not cid:
            continue
        nodes[cid] = {
            "id": cid,
            "label": ch.get("title") or cid,
            "subscribers": ch.get("subscriber_count") or 0,
            "category": _category(ch.get("discovery_method")),
            "resolved": True,
        }

    norm_edges: list[dict[str, Any]] = []
    degree: dict[str, int] = {}
    for e in edges:
        source = e.get("source_channel_id", "")
        if not source:
            continue
        if source not in nodes:
            # An edge's source should have been hydrated (it was expanded to
            # produce the edge), but a partial/interrupted run can leave one
            # dangling — represent it rather than dropping the edge.
            nodes[source] = {
                "id": source, "label": source, "subscribers": 0,
                "category": "unresolved", "resolved": False,
            }

        target_id = e.get("target_channel_id")
        target_ref = e.get("target_channel_ref", "")
        if target_id and target_id in nodes:
            target = target_id
        elif target_ref:
            target = target_ref
            if target not in nodes:
                nodes[target] = {
                    "id": target, "label": _ref_label(target_ref),
                    "subscribers": 0, "category": "unresolved", "resolved": False,
                }
        else:
            continue

        norm_edges.append({
            "source": source, "target": target,
            "edge_type": e.get("edge_type", ""),
        })
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1

    node_list = list(nodes.values())
    if max_nodes > 0 and len(node_list) > max_nodes:
        # Trim unresolved nodes first, lowest-degree first — a frontier node
        # with a single edge and no metadata adds the least to the picture.
        # A hydrated channel is never dropped by this cap.
        resolved = [n for n in node_list if n["resolved"]]
        unresolved = sorted(
            (n for n in node_list if not n["resolved"]),
            key=lambda n: degree.get(n["id"], 0),
            reverse=True,
        )
        keep = set(n["id"] for n in resolved) | {
            n["id"] for n in unresolved[: max(0, max_nodes - len(resolved))]
        }
        node_list = [n for n in node_list if n["id"] in keep]
        norm_edges = [
            e for e in norm_edges if e["source"] in keep and e["target"] in keep
        ]

    counts: dict[str, int] = {}
    for n in node_list:
        counts[n["category"]] = counts.get(n["category"], 0) + 1

    return {
        "nodes": node_list,
        "edges": norm_edges,
        "category_counts": counts,
        "truncated": len(nodes) > len(node_list),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _report_markdown(
    report: dict[str, Any], manifest: dict[str, Any], channels: list[dict]
) -> str:
    grade_order = {"strong": 0, "moderate": 1, "weak": 2}
    findings = sorted(
        report.get("findings", []),
        key=lambda f: grade_order.get(f.get("grade", "weak"), 3),
    )

    lines = [
        f"# {report.get('niche', 'Untitled')} — niche research",
        "",
        f"Run `{manifest['run_id']}` · generated {manifest['exported_at'][:19]}Z",
        "",
        "## How to read this",
        "",
        f"{manifest['channels']} channels and {manifest['videos']} videos were "
        f"discovered and scored. Findings are graded **strong / moderate / weak** "
        "by a deterministic four-axis check — corroboration, consistency, "
        "recency, effect size — not by how confident the writing sounds.",
        "",
    ]

    stop = manifest.get("stop_reasons", [])
    if stop and not all(s == "novelty_below_threshold" for s in stop):
        lines += [
            "> **This run did not reach saturation.** It stopped on "
            f"`{', '.join(sorted(set(stop)))}`, meaning the budget or round "
            "ceiling ended it while new channels were still being found. Treat "
            "this as a survey of what was reached, not a complete picture of "
            "the niche.",
            "",
        ]

    lines += ["## Summary", "", report.get("summary", "_No summary._"), "", "## Findings", ""]
    for finding in findings:
        lines.append(f"### [{finding.get('grade', '?').upper()}] {finding.get('claim', '')}")
        evidence = finding.get("evidence", {})
        if evidence:
            lines.append(
                f"*Corroboration: {evidence.get('corroboration', '?')} · "
                f"consistency: {evidence.get('consistency', '?')} · "
                f"recency: {evidence.get('recency', '?')} · "
                f"effect size: {evidence.get('effect_size', '?')}*"
            )
        supporting = finding.get("supporting_channel_ids", [])
        if supporting:
            by_id = {c["channel_id"]: c.get("title") or c["channel_id"] for c in channels}
            named = [by_id.get(cid, cid) for cid in supporting[:6]]
            lines.append(f"Channels: {', '.join(named)}")
        lines.append("")

    cannot = report.get("cannot_determine", [])
    if cannot:
        lines += ["## What this data cannot tell you", ""]
        lines += [f"- {item}" for item in cannot]
        lines.append("")

    lines += [
        "## Top channels by audience",
        "",
        "| Channel | Subscribers | Found by | Engagement | Uploads/30d |",
        "|---|---:|---|---:|---:|",
    ]
    for ch in channels[:20]:
        eng = ch.get("engagement_rate")
        cad = ch.get("cadence")
        lines.append(
            f"| {ch.get('title') or ch['channel_id']} "
            f"| {ch.get('subscriber_count') or 0:,} "
            f"| {ch.get('discovery_method', '?')} "
            f"| {eng if eng is not None else '—'} "
            f"| {cad if cad is not None else '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


_GRAPH_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — discovery graph</title>
<style>
  :root {
    --surface:      #fcfcfb;
    --surface-2:    #f2f1ee;
    --ink:          #0b0b0b;
    --ink-2:        #52514e;
    --ink-3:        #8a887f;
    --rule:         #e3e1da;
    --edge-rgb:     11,11,11;
    --cat-keyword:    #2a78d6;
    --cat-graph_walk: #eb6834;
    --cat-both:       #1baf7a;
    --cat-unresolved: #9a9890;
    --shadow: 0 1px 2px rgba(11,11,11,.06), 0 8px 24px -12px rgba(11,11,11,.18);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --surface:      #1a1a19;
      --surface-2:    #232320;
      --ink:          #ffffff;
      --ink-2:        #c3c2b7;
      --ink-3:        #82817a;
      --rule:         #333230;
      --edge-rgb:     255,255,255;
      --cat-keyword:    #3987e5;
      --cat-graph_walk: #d95926;
      --cat-both:       #199e70;
      --cat-unresolved: #6f6e68;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
    }
  }
  :root[data-theme="dark"] {
    --surface:      #1a1a19;
    --surface-2:    #232320;
    --ink:          #ffffff;
    --ink-2:        #c3c2b7;
    --ink-3:        #82817a;
    --rule:         #333230;
    --edge-rgb:     255,255,255;
    --cat-keyword:    #3987e5;
    --cat-graph_walk: #d95926;
    --cat-both:       #199e70;
    --cat-unresolved: #6f6e68;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--surface);
    color: var(--ink);
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
  }
  header {
    padding: .875rem 1.25rem;
    border-bottom: 1px solid var(--rule);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: .5rem 1.5rem;
    background: var(--surface);
    z-index: 5;
  }
  .title-block { flex: 1 1 auto; min-width: 12rem; }
  h1 {
    margin: 0;
    font-size: 1.0625rem;
    font-weight: 650;
    letter-spacing: -.01em;
  }
  .byline {
    margin: .125rem 0 0;
    font-size: .75rem;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: .75rem 1.25rem;
  }
  .legend { display: flex; flex-wrap: wrap; gap: .625rem 1rem; }
  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: .375rem;
    font-size: .75rem;
    color: var(--ink-2);
    white-space: nowrap;
  }
  .dot {
    width: .625rem; height: .625rem; border-radius: 50%;
    flex: none;
    box-shadow: 0 0 0 1px rgba(var(--edge-rgb), .12) inset;
  }
  .dot.hollow { background: transparent; border: 1.5px dashed var(--cat-unresolved); }
  .field {
    display: inline-flex;
    align-items: center;
    gap: .375rem;
    font-size: .75rem;
    color: var(--ink-2);
  }
  input[type="search"] {
    font: inherit;
    font-size: .8125rem;
    padding: .3rem .55rem;
    border-radius: 5px;
    border: 1px solid var(--rule);
    background: var(--surface-2);
    color: var(--ink);
    width: 11rem;
  }
  input[type="search"]:focus-visible, button:focus-visible {
    outline: 2px solid var(--cat-keyword);
    outline-offset: 1px;
  }
  button {
    font: inherit;
    font-size: .75rem;
    padding: .35rem .65rem;
    border-radius: 5px;
    border: 1px solid var(--rule);
    background: var(--surface-2);
    color: var(--ink-2);
    cursor: pointer;
  }
  button[aria-pressed="true"] { color: var(--ink); border-color: var(--cat-keyword); }
  main { position: relative; flex: 1 1 auto; min-height: 0; }
  #canvas-wrap { position: absolute; inset: 0; }
  canvas { display: block; width: 100%; height: 100%; cursor: grab; }
  canvas.dragging { cursor: grabbing; }
  #tooltip {
    position: absolute;
    pointer-events: none;
    max-width: 15rem;
    padding: .5rem .625rem;
    border-radius: 6px;
    background: var(--surface);
    border: 1px solid var(--rule);
    box-shadow: var(--shadow);
    font-size: .75rem;
    color: var(--ink-2);
    opacity: 0;
    transform: translate(-9999px, -9999px);
  }
  #tooltip strong { display: block; color: var(--ink); font-size: .8125rem; margin-bottom: .125rem; }
  #tooltip .tt-row { display: flex; justify-content: space-between; gap: .75rem; }
  #empty-state {
    position: absolute; inset: 0;
    display: none;
    align-items: center; justify-content: center;
    color: var(--ink-3); font-size: .875rem;
  }
  #table-view {
    position: absolute; inset: 0;
    overflow: auto;
    background: var(--surface);
    display: none;
  }
  #table-view table { width: 100%; border-collapse: collapse; font-size: .8125rem; }
  #table-view th {
    position: sticky; top: 0;
    text-align: left;
    font-size: .6875rem; letter-spacing: .07em; text-transform: uppercase;
    color: var(--ink-3); font-weight: 500;
    padding: .5rem .75rem;
    border-bottom: 1px solid var(--rule);
    background: var(--surface);
    cursor: pointer;
    white-space: nowrap;
  }
  #table-view td {
    padding: .4rem .75rem;
    border-bottom: 1px solid var(--rule);
    color: var(--ink-2);
  }
  #table-view td:first-child { color: var(--ink); }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  #hint {
    position: absolute; left: 1rem; bottom: 1rem;
    font-size: .6875rem; color: var(--ink-3);
    background: var(--surface); padding: .25rem .5rem; border-radius: 4px;
    border: 1px solid var(--rule);
    pointer-events: none;
  }
</style>
</head>
<body>
  <header>
    <div class="title-block">
      <h1>__TITLE__ — discovery graph</h1>
      <p class="byline">__BYLINE__</p>
    </div>
    <div class="controls">
      <div class="legend" id="legend"></div>
      <label class="field"><input type="checkbox" id="toggle-unresolved" checked> Show frontier nodes</label>
      <input type="search" id="search" placeholder="Find a channel…" aria-label="Find a channel">
      <button id="btn-reset" type="button">Reset view</button>
      <button id="btn-table" type="button" aria-pressed="false">View as table</button>
      <button id="btn-theme" type="button" aria-pressed="false">Dark</button>
    </div>
  </header>
  <main>
    <div id="canvas-wrap">
      <canvas id="canvas"></canvas>
      <div id="empty-state">No nodes match the current filter.</div>
    </div>
    <div id="table-view">
      <table>
        <thead><tr>
          <th data-key="label">Channel</th>
          <th data-key="category">Found by</th>
          <th data-key="subscribers" class="n">Subscribers</th>
          <th data-key="resolved">Fetched</th>
        </tr></thead>
        <tbody id="table-body"></tbody>
      </table>
    </div>
    <div id="tooltip"></div>
    <div id="hint">Drag to move · scroll to zoom · click a node to focus its neighbours</div>
  </main>

<script type="application/json" id="graph-data">__GRAPH_JSON__</script>
<script>
(function () {
  "use strict";
  var payload = JSON.parse(document.getElementById("graph-data").textContent);
  var CATS = ["keyword", "graph_walk", "both", "unresolved"];
  var CAT_LABEL = {
    keyword: "Keyword only", graph_walk: "Graph walk only",
    both: "Both tracks", unresolved: "Unresolved (frontier)"
  };

  // ---- theme -------------------------------------------------------------
  var root = document.documentElement;
  var themeBtn = document.getElementById("btn-theme");
  function applyThemeButtonLabel() {
    var explicit = root.getAttribute("data-theme");
    themeBtn.textContent = explicit === "dark" ? "Light" : "Dark";
    themeBtn.setAttribute("aria-pressed", explicit === "dark" ? "true" : "false");
  }
  themeBtn.addEventListener("click", function () {
    var current = root.getAttribute("data-theme");
    if (current === "dark") { root.setAttribute("data-theme", "light"); }
    else if (current === "light") { root.removeAttribute("data-theme"); }
    else { root.setAttribute("data-theme", "dark"); }
    applyThemeButtonLabel();
    readTokens();
    render();
  });
  applyThemeButtonLabel();

  // ---- read CSS tokens (so canvas colors always match the live theme) ---
  var tok = {};
  function readTokens() {
    var s = getComputedStyle(document.body);
    ["--surface", "--ink", "--ink-2", "--ink-3", "--rule", "--edge-rgb",
     "--cat-keyword", "--cat-graph_walk", "--cat-both", "--cat-unresolved"
    ].forEach(function (k) { tok[k] = s.getPropertyValue(k).trim(); });
  }
  readTokens();
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      readTokens(); render();
    });
  }

  // ---- legend + filters ---------------------------------------------------
  var legend = document.getElementById("legend");
  CATS.forEach(function (cat) {
    var count = payload.category_counts[cat] || 0;
    var item = document.createElement("span");
    item.className = "legend-item";
    var dot = document.createElement("span");
    dot.className = "dot" + (cat === "unresolved" ? " hollow" : "");
    if (cat !== "unresolved") { dot.style.background = "var(--cat-" + cat + ")"; }
    item.appendChild(dot);
    item.appendChild(document.createTextNode(CAT_LABEL[cat] + " (" + count.toLocaleString() + ")"));
    legend.appendChild(item);
  });
  if (payload.truncated) {
    var note = document.createElement("span");
    note.className = "legend-item";
    note.style.color = "var(--ink-3)";
    note.textContent = "· lowest-connected frontier nodes trimmed for display";
    legend.appendChild(note);
  }

  // ---- graph data ----------------------------------------------------------
  var spread = 40 * Math.sqrt(Math.max(1, payload.nodes.length));
  var nodes = payload.nodes.map(function (n, i) {
    var a = (i * 2.399963) % (Math.PI * 2); // golden-angle spiral: no initial overlap
    var r = spread * Math.sqrt((i + 1) / payload.nodes.length);
    return {
      id: n.id, label: n.label, subscribers: n.subscribers || 0,
      category: n.category, resolved: n.resolved,
      x: Math.cos(a) * r, y: Math.sin(a) * r,
      vx: 0, vy: 0, fx: null, fy: null, hidden: false
    };
  });
  var byId = new Map(nodes.map(function (n) { return [n.id, n]; }));
  var edges = payload.edges.map(function (e) {
    return { source: byId.get(e.source), target: byId.get(e.target), edge_type: e.edge_type };
  }).filter(function (e) { return e.source && e.target; });

  var subMax = nodes.reduce(function (m, n) { return Math.max(m, n.subscribers); }, 1);
  function radius(n) {
    var v = Math.sqrt(n.subscribers / subMax);
    return 3 + v * 15;
  }

  var EDGE_LEN = {
    featured_channel: 42, recommendation: 68, playlist: 68,
    comment_author: 95, collaboration: 95, comment_mention: 95
  };
  var EDGE_STR = {
    featured_channel: 0.95, recommendation: 0.65, playlist: 0.65,
    comment_author: 0.35, collaboration: 0.35, comment_mention: 0.35
  };

  // ---- spatial-grid force simulation ---------------------------------------
  // O(n) repulsion via a uniform grid rather than O(n^2) all-pairs — this
  // graph runs to ~2,000 nodes, and all-pairs is not viable at that count.
  var CELL = 90, REPEL = 480, CENTER_K = 0.018, DAMPING = 0.82;
  var alpha = 1, alphaTarget = 0;
  var alphaDecay = 1 - Math.pow(0.001, 1 / 260);
  var draggedNode = null;

  function key(x, y) { return (Math.floor(x / CELL)) + "," + (Math.floor(y / CELL)); }

  function tick() {
    var active = nodes.filter(function (n) { return !n.hidden; });
    var grid = new Map();
    for (var i = 0; i < active.length; i++) {
      var n = active[i], k = key(n.x, n.y);
      var arr = grid.get(k);
      if (!arr) { arr = []; grid.set(k, arr); }
      arr.push(n);
    }
    var cutoff2 = (CELL * 1.6) * (CELL * 1.6);
    for (var j = 0; j < active.length; j++) {
      var node = active[j];
      if (node === draggedNode || node.fx !== null) continue;
      var fx = 0, fy = 0;
      var cx = Math.floor(node.x / CELL), cy = Math.floor(node.y / CELL);
      for (var dx = -1; dx <= 1; dx++) {
        for (var dy = -1; dy <= 1; dy++) {
          var bucket = grid.get((cx + dx) + "," + (cy + dy));
          if (!bucket) continue;
          for (var b = 0; b < bucket.length; b++) {
            var o = bucket[b];
            if (o === node) continue;
            var ddx = node.x - o.x, ddy = node.y - o.y;
            var d2 = ddx * ddx + ddy * ddy;
            if (d2 > cutoff2) continue;
            if (d2 < 4) { ddx = (Math.random() - 0.5); ddy = (Math.random() - 0.5); d2 = 4; }
            var d = Math.sqrt(d2);
            var f = REPEL / d2;
            fx += (ddx / d) * f; fy += (ddy / d) * f;
          }
        }
      }
      fx += -node.x * CENTER_K;
      fy += -node.y * CENTER_K;
      node.vx = (node.vx + fx * alpha) * DAMPING;
      node.vy = (node.vy + fy * alpha) * DAMPING;
    }
    for (var e = 0; e < edges.length; e++) {
      var edge = edges[e];
      if (edge.source.hidden || edge.target.hidden) continue;
      var strength = EDGE_STR[edge.edge_type] || 0.5;
      var targetLen = EDGE_LEN[edge.edge_type] || 85;
      var a = edge.source, bN = edge.target;
      var edx = bN.x - a.x, edy = bN.y - a.y;
      var ed = Math.sqrt(edx * edx + edy * edy) || 0.01;
      var diff = ((ed - targetLen) / ed) * strength * alpha;
      var mx = edx * diff * 0.5, my = edy * diff * 0.5;
      if (a !== draggedNode && a.fx === null) { a.vx += mx; a.vy += my; }
      if (bN !== draggedNode && bN.fx === null) { bN.vx -= mx; bN.vy -= my; }
    }
    for (var m = 0; m < active.length; m++) {
      var nn = active[m];
      if (nn === draggedNode || nn.fx !== null) continue;
      nn.x += nn.vx; nn.y += nn.vy;
    }
    alpha += (alphaTarget - alpha) * alphaDecay;
  }
  function wake(strength) { alpha = Math.max(alpha, strength == null ? 0.35 : strength); loop(); }

  // ---- canvas + transform ---------------------------------------------------
  var wrap = document.getElementById("canvas-wrap");
  var canvas = document.getElementById("canvas");
  var ctx = canvas.getContext("2d");
  var transform = { x: 0, y: 0, k: 1 };
  var dpr = Math.max(1, window.devicePixelRatio || 1);

  function resize() {
    var rect = wrap.getBoundingClientRect();
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    canvas.style.width = rect.width + "px";
    canvas.style.height = rect.height + "px";
    if (!transform.initialised) {
      transform.x = rect.width / 2; transform.y = rect.height / 2;
      transform.initialised = true;
    }
    render();
  }
  window.addEventListener("resize", resize);

  function toScreen(n) { return [n.x * transform.k + transform.x, n.y * transform.k + transform.y]; }
  function toWorld(sx, sy) { return [(sx - transform.x) / transform.k, (sy - transform.y) / transform.k]; }

  // ---- selection / hover state ------------------------------------------
  var hovered = null, focused = null, matched = new Set();

  function neighbourSet(n) {
    var s = new Set([n.id]);
    edges.forEach(function (e) {
      if (e.source === n) s.add(e.target.id);
      if (e.target === n) s.add(e.source.id);
    });
    return s;
  }

  function render() {
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr);
    ctx.fillStyle = tok["--surface"];
    ctx.fillRect(0, 0, canvas.width / dpr, canvas.height / dpr);

    var dim = focused ? neighbourSet(focused) : (matched.size ? matched : null);

    ctx.lineWidth = 1;
    edges.forEach(function (e) {
      if (e.source.hidden || e.target.hidden) return;
      var a = toScreen(e.source), b = toScreen(e.target);
      var faded = dim && !(dim.has(e.source.id) && dim.has(e.target.id));
      ctx.strokeStyle = "rgba(" + tok["--edge-rgb"] + "," + (faded ? 0.05 : 0.16) + ")";
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    });

    nodes.forEach(function (n) {
      if (n.hidden) return;
      var p = toScreen(n);
      var r = radius(n) * transform.k;
      if (p[0] < -r - 20 || p[0] > canvas.width / dpr + r + 20) return;
      if (p[1] < -r - 20 || p[1] > canvas.height / dpr + r + 20) return;
      var faded = dim && !dim.has(n.id);
      var color = n.category === "unresolved" ? tok["--cat-unresolved"] : tok["--cat-" + n.category];
      ctx.globalAlpha = faded ? 0.22 : 1;
      ctx.beginPath();
      ctx.arc(p[0], p[1], Math.max(1.5, r), 0, Math.PI * 2);
      if (n.category === "unresolved") {
        ctx.fillStyle = tok["--surface"];
        ctx.fill();
        ctx.lineWidth = 1.3;
        ctx.setLineDash([2, 2]);
        ctx.strokeStyle = color;
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = tok["--surface"];
        ctx.stroke();
      }
      if (n === hovered || n === focused || matched.has(n.id)) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = tok["--ink"];
        ctx.beginPath();
        ctx.arc(p[0], p[1], Math.max(1.5, r) + 2, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    });

    // direct label for hovered/focused/matched — selective, not every node
    var labelTargets = [];
    if (hovered) labelTargets.push(hovered);
    if (focused) labelTargets.push(focused);
    if (matched.size && matched.size <= 40) {
      nodes.forEach(function (n) { if (matched.has(n.id)) labelTargets.push(n); });
    }
    ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
    ctx.textBaseline = "middle";
    labelTargets.forEach(function (n) {
      var p = toScreen(n);
      var r = radius(n) * transform.k;
      var text = n.label;
      var tw = ctx.measureText(text).width;
      ctx.fillStyle = "rgba(" + (tok["--surface"] === "#1a1a19" ? "26,26,25" : "252,252,251") + ",.92)";
      ctx.fillRect(p[0] + r + 5, p[1] - 9, tw + 8, 18);
      ctx.fillStyle = tok["--ink"];
      ctx.fillText(text, p[0] + r + 9, p[1] + 1);
    });

    ctx.restore();
  }

  var rafRunning = false;
  function loop() {
    if (rafRunning) return;
    rafRunning = true;
    function frame() {
      tick(); render();
      if (alpha > 0.001 || draggedNode) {
        requestAnimationFrame(frame);
      } else {
        rafRunning = false;
      }
    }
    requestAnimationFrame(frame);
  }

  // ---- interaction: drag / pan / zoom -------------------------------------
  var panning = false, panStart = null, dragMoved = false;
  var DRAG_THRESHOLD = 4;

  function nodeAt(sx, sy) {
    var w = toWorld(sx, sy);
    var best = null, bestD = Infinity;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.hidden) continue;
      var dx = w[0] - n.x, dy = w[1] - n.y;
      var d = Math.sqrt(dx * dx + dy * dy);
      var hit = radius(n) + 4 / transform.k;
      if (d < hit && d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  canvas.addEventListener("mousedown", function (ev) {
    var rect = canvas.getBoundingClientRect();
    var sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
    var n = nodeAt(sx, sy);
    dragMoved = false;
    if (n) {
      draggedNode = n; canvas.classList.add("dragging");
      panStart = { sx: sx, sy: sy };
    } else {
      panning = true; canvas.classList.add("dragging");
      panStart = { sx: sx, sy: sy, tx: transform.x, ty: transform.y };
    }
  });
  window.addEventListener("mousemove", function (ev) {
    var rect = canvas.getBoundingClientRect();
    var sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
    if (draggedNode) {
      if (Math.abs(sx - panStart.sx) > DRAG_THRESHOLD || Math.abs(sy - panStart.sy) > DRAG_THRESHOLD) dragMoved = true;
      var w = toWorld(sx, sy);
      draggedNode.fx = w[0]; draggedNode.fy = w[1];
      draggedNode.x = w[0]; draggedNode.y = w[1];
      wake(0.4);
    } else if (panning) {
      transform.x = panStart.tx + (sx - panStart.sx);
      transform.y = panStart.ty + (sy - panStart.sy);
      render();
    } else if (sx >= 0 && sy >= 0 && sx <= rect.width && sy <= rect.height) {
      var hit = nodeAt(sx, sy);
      if (hit !== hovered) {
        hovered = hit;
        showTooltip(hit, ev.clientX, ev.clientY);
        render();
      } else if (hit) {
        positionTooltip(ev.clientX, ev.clientY);
      }
    }
  });
  window.addEventListener("mouseup", function () {
    if (draggedNode) {
      var n = draggedNode;
      if (!dragMoved) {
        // a click, not a drag: toggle focus on this node's ego-network
        focused = focused === n ? null : n;
        matched.clear(); searchInput.value = "";
        n.fx = null; n.fy = null;
      } else {
        n.fx = null; n.fy = null;
      }
      draggedNode = null;
      wake(0.3);
      render();
    } else if (panning && !dragMoved) {
      focused = null;
      render();
    }
    panning = false;
    canvas.classList.remove("dragging");
  });
  canvas.addEventListener("mouseleave", function () { hovered = null; hideTooltip(); render(); });

  canvas.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    var w = toWorld(mx, my);
    var delta = -ev.deltaY * 0.0016;
    var newK = Math.min(9, Math.max(0.06, transform.k * (1 + delta)));
    transform.x = mx - w[0] * newK;
    transform.y = my - w[1] * newK;
    transform.k = newK;
    render();
  }, { passive: false });

  // ---- tooltip --------------------------------------------------------------
  var tooltip = document.getElementById("tooltip");
  function showTooltip(n, cx, cy) {
    if (!n) { hideTooltip(); return; }
    tooltip.innerHTML =
      "<strong>" + escapeHtml(n.label) + "</strong>" +
      "<div class=\"tt-row\"><span>Subscribers</span><span>" + n.subscribers.toLocaleString() + "</span></div>" +
      "<div class=\"tt-row\"><span>Found by</span><span>" + CAT_LABEL[n.category] + "</span></div>" +
      "<div class=\"tt-row\"><span>Fetched</span><span>" + (n.resolved ? "yes" : "no — frontier only") + "</span></div>";
    tooltip.style.opacity = 1;
    positionTooltip(cx, cy);
  }
  function positionTooltip(cx, cy) {
    var wrapRect = wrap.getBoundingClientRect();
    tooltip.style.transform = "translate(" + (cx - wrapRect.left + 14) + "px," + (cy - wrapRect.top + 14) + "px)";
  }
  function hideTooltip() { tooltip.style.opacity = 0; tooltip.style.transform = "translate(-9999px,-9999px)"; }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- filters: hide frontier nodes ---------------------------------------
  var toggleUnresolved = document.getElementById("toggle-unresolved");
  var emptyState = document.getElementById("empty-state");
  function applyVisibility() {
    var showUnresolved = toggleUnresolved.checked;
    var visibleCount = 0;
    nodes.forEach(function (n) {
      n.hidden = (!showUnresolved && n.category === "unresolved");
      if (!n.hidden) visibleCount++;
    });
    emptyState.style.display = visibleCount === 0 ? "flex" : "none";
    wake(0.5);
    render();
  }
  toggleUnresolved.addEventListener("change", applyVisibility);

  // ---- search -----------------------------------------------------------
  var searchInput = document.getElementById("search");
  searchInput.addEventListener("input", function () {
    var q = searchInput.value.trim().toLowerCase();
    matched.clear();
    focused = null;
    if (q.length >= 2) {
      nodes.forEach(function (n) {
        if (!n.hidden && n.label.toLowerCase().indexOf(q) !== -1) matched.add(n.id);
      });
    }
    render();
  });

  // ---- reset view ---------------------------------------------------------
  document.getElementById("btn-reset").addEventListener("click", function () {
    var rect = wrap.getBoundingClientRect();
    transform.x = rect.width / 2; transform.y = rect.height / 2; transform.k = 1;
    focused = null; matched.clear(); searchInput.value = "";
    render();
  });

  // ---- table view ---------------------------------------------------------
  var tableBtn = document.getElementById("btn-table");
  var tableView = document.getElementById("table-view");
  var tableBody = document.getElementById("table-body");
  var tableSort = { key: "subscribers", dir: -1 };
  function renderTable() {
    var rows = payload.nodes.slice().sort(function (a, b) {
      var k = tableSort.key, dir = tableSort.dir;
      var av = a[k], bv = b[k];
      if (typeof av === "string") { av = av.toLowerCase(); bv = (bv || "").toLowerCase(); }
      return av < bv ? -1 * dir : av > bv ? 1 * dir : 0;
    });
    tableBody.innerHTML = rows.map(function (n) {
      return "<tr><td>" + escapeHtml(n.label) + "</td><td>" + CAT_LABEL[n.category] +
        "</td><td class=\"n\">" + n.subscribers.toLocaleString() + "</td><td>" +
        (n.resolved ? "yes" : "no") + "</td></tr>";
    }).join("");
  }
  document.querySelectorAll("#table-view th").forEach(function (th) {
    th.addEventListener("click", function () {
      var k = th.getAttribute("data-key");
      tableSort.dir = tableSort.key === k ? -tableSort.dir : -1;
      tableSort.key = k;
      renderTable();
    });
  });
  tableBtn.addEventListener("click", function () {
    var showing = tableBtn.getAttribute("aria-pressed") === "true";
    tableBtn.setAttribute("aria-pressed", showing ? "false" : "true");
    tableBtn.textContent = showing ? "View as table" : "View as graph";
    if (!showing) { renderTable(); tableView.style.display = "block"; }
    else { tableView.style.display = "none"; }
  });

  resize();
  applyVisibility();
  wake(1);
})();
</script>
</body>
</html>
"""


def _interactive_graph_html(payload: dict[str, Any], manifest: dict[str, Any]) -> str:
    niche = manifest.get("niche") or "Untitled"
    n_nodes = len(payload.get("nodes", []))
    n_edges = len(payload.get("edges", []))
    byline = (
        f"{n_nodes:,} nodes · {n_edges:,} edges · run {manifest.get('run_id', '')} · "
        f"generated {manifest.get('exported_at', '')[:10]}"
    )
    graph_json = json.dumps(payload, default=str).replace("</", "<\\/")
    html = _GRAPH_HTML_TEMPLATE
    html = html.replace("__TITLE__", niche)
    html = html.replace("__BYLINE__", byline)
    html = html.replace("__GRAPH_JSON__", graph_json)
    return html


_TAXONOMY_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — taxonomy tree</title>
<style>
  :root {
    --surface:      #fcfcfb;
    --surface-2:    #f2f1ee;
    --ink:          #0b0b0b;
    --ink-2:        #52514e;
    --ink-3:        #8a887f;
    --rule:         #e3e1da;
    --edge-rgb:     11,11,11;
    --st-active:      #eb6834;
    --st-saturated:   #2a78d6;
    --st-compacted:   #1baf7a;
    --st-pending:     #9a9890;
    --shadow: 0 1px 2px rgba(11,11,11,.06), 0 8px 24px -12px rgba(11,11,11,.18);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --surface: #1a1a19; --surface-2: #232320; --ink: #ffffff;
      --ink-2: #c3c2b7; --ink-3: #82817a; --rule: #333230; --edge-rgb: 255,255,255;
      --st-active: #d95926; --st-saturated: #3987e5; --st-compacted: #199e70; --st-pending: #6f6e68;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
    }
  }
  :root[data-theme="dark"] {
    --surface: #1a1a19; --surface-2: #232320; --ink: #ffffff;
    --ink-2: #c3c2b7; --ink-3: #82817a; --rule: #333230; --edge-rgb: 255,255,255;
    --st-active: #d95926; --st-saturated: #3987e5; --st-compacted: #199e70; --st-pending: #6f6e68;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--surface); color: var(--ink);
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    padding: .875rem 1.25rem; border-bottom: 1px solid var(--rule);
    display: flex; flex-wrap: wrap; align-items: center; gap: .5rem 1.5rem;
    background: var(--surface); z-index: 5;
  }
  .title-block { flex: 1 1 auto; min-width: 12rem; }
  h1 { margin: 0; font-size: 1.0625rem; font-weight: 650; letter-spacing: -.01em; }
  .byline { margin: .125rem 0 0; font-size: .75rem; color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .controls { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem 1.25rem; }
  .legend { display: flex; flex-wrap: wrap; gap: .625rem 1rem; }
  .legend-item { display: inline-flex; align-items: center; gap: .375rem; font-size: .75rem; color: var(--ink-2); white-space: nowrap; }
  .dot { width: .625rem; height: .625rem; border-radius: 50%; flex: none; box-shadow: 0 0 0 1px rgba(var(--edge-rgb), .12) inset; }
  .dot.hollow { background: transparent; border: 1.5px dashed var(--st-pending); }
  .field { display: inline-flex; align-items: center; gap: .375rem; font-size: .75rem; color: var(--ink-2); }
  input[type="search"] {
    font: inherit; font-size: .8125rem; padding: .3rem .55rem; border-radius: 5px;
    border: 1px solid var(--rule); background: var(--surface-2); color: var(--ink); width: 11rem;
  }
  input[type="search"]:focus-visible, button:focus-visible { outline: 2px solid var(--st-saturated); outline-offset: 1px; }
  button {
    font: inherit; font-size: .75rem; padding: .35rem .65rem; border-radius: 5px;
    border: 1px solid var(--rule); background: var(--surface-2); color: var(--ink-2); cursor: pointer;
  }
  button[aria-pressed="true"] { color: var(--ink); border-color: var(--st-saturated); }
  main { position: relative; flex: 1 1 auto; min-height: 0; }
  #tree-wrap { position: absolute; inset: 0; overflow: hidden; cursor: grab; }
  #tree-wrap.dragging { cursor: grabbing; }
  svg { display: block; width: 100%; height: 100%; }
  .node-g { cursor: pointer; }
  .node-g .card {
    fill: var(--surface); stroke: var(--rule); stroke-width: 1.5px;
    filter: drop-shadow(0 1px 1px rgba(var(--edge-rgb), .12));
  }
  .node-g[data-status="active"] .card { stroke: var(--st-active); stroke-width: 2px; }
  .node-g[data-status="saturated"] .card { stroke: var(--st-saturated); stroke-width: 2px; }
  .node-g[data-status="compacted"] .card { stroke: var(--st-compacted); stroke-width: 2px; }
  .node-g[data-status="pending"] .card { stroke: var(--st-pending); stroke-width: 1.5px; stroke-dasharray: 3 3; }
  .node-g.focused .card, .node-g.matched .card { stroke-width: 3px; stroke: var(--ink); }
  .node-g.dimmed { opacity: .28; }
  .node-label { fill: var(--ink); font-size: 12.5px; font-weight: 600; }
  .node-sub { fill: var(--ink-3); font-size: 10.5px; }
  .node-status-dot { }
  .edge {
    fill: none; stroke: rgba(var(--edge-rgb), .22); stroke-width: 1.5px;
  }
  .toggle-btn circle { fill: var(--surface-2); stroke: var(--rule); }
  .toggle-btn text { fill: var(--ink-2); font-size: 11px; text-anchor: middle; dominant-baseline: central; }
  #hint {
    position: absolute; left: 1rem; bottom: 1rem; font-size: .6875rem; color: var(--ink-3);
    background: var(--surface); padding: .25rem .5rem; border-radius: 4px; border: 1px solid var(--rule);
    pointer-events: none;
  }
  #tooltip {
    position: absolute; pointer-events: none; max-width: 16rem; padding: .5rem .625rem;
    border-radius: 6px; background: var(--surface); border: 1px solid var(--rule); box-shadow: var(--shadow);
    font-size: .75rem; color: var(--ink-2); opacity: 0; transform: translate(-9999px,-9999px);
  }
  #tooltip strong { display: block; color: var(--ink); font-size: .8125rem; margin-bottom: .125rem; }
  #tooltip .tt-row { display: flex; justify-content: space-between; gap: .75rem; }
  #panel {
    position: absolute; top: 0; right: 0; bottom: 0; width: 23rem; max-width: 88vw;
    background: var(--surface); border-left: 1px solid var(--rule); box-shadow: var(--shadow);
    padding: 1.25rem; overflow-y: auto; transform: translateX(100%); transition: transform .2s ease;
  }
  #panel.open { transform: translateX(0); }
  #panel h2 { margin: 0 0 .25rem; font-size: 1rem; }
  #panel .pill {
    display: inline-block; font-size: .6875rem; padding: .15rem .5rem; border-radius: 999px;
    border: 1px solid var(--rule); color: var(--ink-2); margin: .375rem .375rem .375rem 0;
  }
  #panel .stat-row { display: flex; gap: 1.25rem; margin: .75rem 0; }
  #panel .stat { font-size: 1.125rem; font-weight: 650; }
  #panel .stat span { display: block; font-size: .6875rem; font-weight: 400; color: var(--ink-3); }
  #panel .kw { display: flex; flex-wrap: wrap; gap: .3rem; margin: .5rem 0 1rem; }
  #panel .kw span {
    font-size: .6875rem; background: var(--surface-2); border-radius: 4px; padding: .15rem .4rem; color: var(--ink-2);
  }
  #panel .summary { font-size: .8125rem; color: var(--ink-2); white-space: pre-wrap; line-height: 1.6; }
  #panel .close { position: absolute; top: .75rem; right: .75rem; }
  #table-view {
    position: absolute; inset: 0; overflow: auto; background: var(--surface); display: none;
  }
  #table-view table { width: 100%; border-collapse: collapse; font-size: .8125rem; }
  #table-view th {
    position: sticky; top: 0; text-align: left; font-size: .6875rem; letter-spacing: .07em;
    text-transform: uppercase; color: var(--ink-3); font-weight: 500; padding: .5rem .75rem;
    border-bottom: 1px solid var(--rule); background: var(--surface); cursor: pointer; white-space: nowrap;
  }
  #table-view td { padding: .4rem .75rem; border-bottom: 1px solid var(--rule); color: var(--ink-2); }
  #table-view td:first-child { color: var(--ink); }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
  <header>
    <div class="title-block">
      <h1>__TITLE__ — taxonomy tree</h1>
      <p class="byline">__BYLINE__</p>
    </div>
    <div class="controls">
      <div class="legend" id="legend"></div>
      <input type="search" id="search" placeholder="Find a node…" aria-label="Find a node">
      <button id="btn-expand" type="button">Expand all</button>
      <button id="btn-collapse" type="button">Collapse all</button>
      <button id="btn-reset" type="button">Fit to view</button>
      <button id="btn-table" type="button" aria-pressed="false">View as table</button>
      <button id="btn-theme" type="button" aria-pressed="false">Dark</button>
    </div>
  </header>
  <main>
    <div id="tree-wrap">
      <svg id="svg" xmlns="http://www.w3.org/2000/svg">
        <g id="edges"></g>
        <g id="nodes"></g>
      </svg>
    </div>
    <div id="table-view">
      <table>
        <thead><tr>
          <th data-key="label">Node</th>
          <th data-key="depth" class="n">Depth</th>
          <th data-key="status">Status</th>
          <th data-key="channel_count" class="n">Channels</th>
          <th data-key="video_count" class="n">Videos</th>
          <th data-key="split_method">Split method</th>
          <th data-key="saturation_reason">Stopped on</th>
        </tr></thead>
        <tbody id="table-body"></tbody>
      </table>
    </div>
    <div id="tooltip"></div>
    <div id="panel">
      <button class="close" id="panel-close" type="button">Close</button>
      <div id="panel-body"></div>
    </div>
    <div id="hint">Drag a node to reposition · scroll to zoom · click a node for its full summary</div>
  </main>

<script type="application/json" id="tree-data">__TREE_JSON__</script>
<script>
(function () {
  "use strict";
  var payload = JSON.parse(document.getElementById("tree-data").textContent);
  var byId = new Map(payload.nodes.map(function (n) { return [n.id, n]; }));
  var STATUS_LABEL = {
    pending: "Pending", active: "Active",
    saturated: "Saturated (awaiting compaction)", compacted: "Compacted"
  };
  var STATUSES = ["compacted", "saturated", "active", "pending"];

  // ---- theme ---------------------------------------------------------------
  var root = document.documentElement;
  var themeBtn = document.getElementById("btn-theme");
  function applyThemeButtonLabel() {
    var explicit = root.getAttribute("data-theme");
    themeBtn.textContent = explicit === "dark" ? "Light" : "Dark";
    themeBtn.setAttribute("aria-pressed", explicit === "dark" ? "true" : "false");
  }
  themeBtn.addEventListener("click", function () {
    var current = root.getAttribute("data-theme");
    if (current === "dark") { root.setAttribute("data-theme", "light"); }
    else if (current === "light") { root.removeAttribute("data-theme"); }
    else { root.setAttribute("data-theme", "dark"); }
    applyThemeButtonLabel();
  });
  applyThemeButtonLabel();

  // ---- legend ---------------------------------------------------------------
  var legend = document.getElementById("legend");
  STATUSES.forEach(function (st) {
    var count = payload.status_counts[st] || 0;
    var item = document.createElement("span");
    item.className = "legend-item";
    var dot = document.createElement("span");
    dot.className = "dot" + (st === "pending" ? " hollow" : "");
    if (st !== "pending") { dot.style.background = "var(--st-" + st + ")"; }
    item.appendChild(dot);
    item.appendChild(document.createTextNode(STATUS_LABEL[st] + " (" + count + ")"));
    legend.appendChild(item);
  });

  // ---- collapse state + layout ----------------------------------------------
  var collapsed = new Set();
  var manualPos = new Map(); // node id -> {x,y} drag overrides

  var BOX_W = 176, BOX_H = 56, H_PITCH = 208, V_PITCH = 128;

  function layout() {
    var nextSlot = 0;
    var flat = [];
    function visit(id, depth, parent) {
      var n = byId.get(id);
      var childIds = collapsed.has(id) ? [] : (payload.children[id] || []);
      var hasChildren = (payload.children[id] || []).length > 0;
      var entry = { id: id, node: n, depth: depth, parent: parent, hasChildren: hasChildren, expanded: !collapsed.has(id) };
      flat.push(entry);
      if (childIds.length === 0) {
        entry.x = nextSlot * H_PITCH;
        nextSlot++;
      } else {
        var firstIdx = flat.length - 1;
        childIds.forEach(function (cid) { visit(cid, depth + 1, entry); });
        var kids = flat.filter(function (e) { return e.parent === entry; });
        var xs = kids.map(function (e) { return e.x; });
        entry.x = (Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2;
      }
      entry.y = depth * V_PITCH;
    }
    payload.roots.forEach(function (rid) { visit(rid, 0, null); });
    flat.forEach(function (e) {
      var override = manualPos.get(e.id);
      if (override) { e.x = override.x; e.y = override.y; }
    });
    return flat;
  }

  // ---- svg + transform --------------------------------------------------
  var wrap = document.getElementById("tree-wrap");
  var svg = document.getElementById("svg");
  var edgesG = document.getElementById("edges");
  var nodesG = document.getElementById("nodes");
  var transform = { x: 40, y: 40, k: 1 };

  function applyTransform() {
    var g = "translate(" + transform.x + "," + transform.y + ") scale(" + transform.k + ")";
    edgesG.setAttribute("transform", g);
    nodesG.setAttribute("transform", g);
  }

  var focusedId = null, matchedIds = new Set();

  function render() {
    var flat = layout();
    var byNodeId = new Map(flat.map(function (e) { return [e.id, e]; }));

    edgesG.innerHTML = "";
    flat.forEach(function (e) {
      if (!e.parent) return;
      var p = e.parent, c = e;
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      var midY = (p.y + BOX_H / 2 + c.y - BOX_H / 2) / 2;
      var d = "M" + (p.x) + "," + (p.y + BOX_H / 2) +
        " C" + p.x + "," + midY + " " + c.x + "," + midY + " " + c.x + "," + (c.y - BOX_H / 2);
      path.setAttribute("d", d);
      path.setAttribute("class", "edge");
      edgesG.appendChild(path);
    });

    nodesG.innerHTML = "";
    var dimActive = focusedId || matchedIds.size;
    var focusSet = focusedId ? ancestorAndDescendantIds(focusedId, byNodeId) : null;

    flat.forEach(function (e) {
      var n = e.node;
      var g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.setAttribute("class", "node-g");
      g.setAttribute("data-status", n.status);
      g.setAttribute("data-id", n.id);
      g.setAttribute("transform", "translate(" + (e.x - BOX_W / 2) + "," + (e.y - BOX_H / 2) + ")");
      if (n.id === focusedId) g.classList.add("focused");
      if (matchedIds.has(n.id)) g.classList.add("matched");
      if (dimActive) {
        var keep = focusedId ? (focusSet && focusSet.has(n.id)) : matchedIds.has(n.id);
        if (!keep) g.classList.add("dimmed");
      }

      var card = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      card.setAttribute("class", "card");
      card.setAttribute("width", BOX_W); card.setAttribute("height", BOX_H);
      card.setAttribute("rx", 8);
      g.appendChild(card);

      var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("class", "node-label");
      label.setAttribute("x", 12); label.setAttribute("y", 22);
      label.textContent = truncate(n.label, 22);
      g.appendChild(label);

      var sub = document.createElementNS("http://www.w3.org/2000/svg", "text");
      sub.setAttribute("class", "node-sub");
      sub.setAttribute("x", 12); sub.setAttribute("y", 40);
      sub.textContent = n.channel_count.toLocaleString() + " ch · " + n.video_count.toLocaleString() + " vid";
      g.appendChild(sub);

      if (e.hasChildren) {
        var tg = document.createElementNS("http://www.w3.org/2000/svg", "g");
        tg.setAttribute("class", "toggle-btn");
        tg.setAttribute("transform", "translate(" + (BOX_W / 2) + "," + BOX_H + ")");
        var circ = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circ.setAttribute("r", 8);
        var txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
        txt.textContent = e.expanded ? "−" : "+";
        tg.appendChild(circ); tg.appendChild(txt);
        tg.addEventListener("mousedown", function (ev) {
          ev.stopPropagation();
          if (collapsed.has(n.id)) collapsed.delete(n.id); else collapsed.add(n.id);
          render();
        });
        g.appendChild(tg);
      }

      g.addEventListener("mouseenter", function (ev) { showTooltip(n, ev.clientX, ev.clientY); });
      g.addEventListener("mousemove", function (ev) { positionTooltip(ev.clientX, ev.clientY); });
      g.addEventListener("mouseleave", hideTooltip);
      g.addEventListener("mousedown", function (ev) { startDrag(ev, e); });
      g.addEventListener("click", function (ev) {
        if (dragMoved) return;
        openPanel(n);
      });

      nodesG.appendChild(g);
    });

    applyTransform();
  }

  function ancestorAndDescendantIds(id, byNodeId) {
    var set = new Set([id]);
    var entry = byNodeId.get(id);
    var p = entry && entry.parent;
    while (p) { set.add(p.id); p = p.parent; }
    (function addDesc(nid) {
      (payload.children[nid] || []).forEach(function (cid) { set.add(cid); addDesc(cid); });
    })(id);
    return set;
  }

  function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  // ---- drag / pan / zoom ---------------------------------------------------
  var dragging = null, dragMoved = false, panStart = null, panning = false;
  var DRAG_THRESHOLD = 4;

  function svgPoint(clientX, clientY) {
    var rect = wrap.getBoundingClientRect();
    var sx = clientX - rect.left, sy = clientY - rect.top;
    return { x: (sx - transform.x) / transform.k, y: (sy - transform.y) / transform.k, sx: sx, sy: sy };
  }

  function startDrag(ev, entry) {
    ev.stopPropagation();
    dragMoved = false;
    var p = svgPoint(ev.clientX, ev.clientY);
    dragging = { entry: entry, offX: p.x - entry.x, offY: p.y - entry.y };
    wrap.classList.add("dragging");
  }

  wrap.addEventListener("mousedown", function (ev) {
    if (dragging) return;
    panning = true; dragMoved = false;
    panStart = { sx: ev.clientX, sy: ev.clientY, tx: transform.x, ty: transform.y };
    wrap.classList.add("dragging");
  });
  window.addEventListener("mousemove", function (ev) {
    if (dragging) {
      var p = svgPoint(ev.clientX, ev.clientY);
      dragMoved = true;
      manualPos.set(dragging.entry.id, { x: p.x - dragging.offX, y: p.y - dragging.offY });
      render();
    } else if (panning) {
      var dx = ev.clientX - panStart.sx, dy = ev.clientY - panStart.sy;
      if (Math.abs(dx) > DRAG_THRESHOLD || Math.abs(dy) > DRAG_THRESHOLD) dragMoved = true;
      transform.x = panStart.tx + dx; transform.y = panStart.ty + dy;
      applyTransform();
    }
  });
  window.addEventListener("mouseup", function () {
    dragging = null; panning = false;
    wrap.classList.remove("dragging");
  });

  wrap.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var rect = wrap.getBoundingClientRect();
    var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    var wx = (mx - transform.x) / transform.k, wy = (my - transform.y) / transform.k;
    var delta = -ev.deltaY * 0.0016;
    var newK = Math.min(4, Math.max(0.15, transform.k * (1 + delta)));
    transform.x = mx - wx * newK; transform.y = my - wy * newK; transform.k = newK;
    applyTransform();
  }, { passive: false });

  // ---- tooltip ---------------------------------------------------------
  var tooltip = document.getElementById("tooltip");
  function showTooltip(n, cx, cy) {
    tooltip.innerHTML =
      "<strong>" + escapeHtml(n.label) + "</strong>" +
      "<div class=\"tt-row\"><span>Status</span><span>" + STATUS_LABEL[n.status] + "</span></div>" +
      "<div class=\"tt-row\"><span>Split method</span><span>" + (n.split_method === "graph_cluster" ? "Graph cluster" : "LLM seed") + "</span></div>" +
      (n.distinctness_score != null ? "<div class=\"tt-row\"><span>Distinctness</span><span>" + n.distinctness_score.toFixed(2) + "</span></div>" : "") +
      "<div class=\"tt-row\"><span>Coverage</span><span>" + n.channel_count + " channels · " + n.video_count + " videos</span></div>" +
      (n.saturation_reason ? "<div class=\"tt-row\"><span>Stopped on</span><span>" + escapeHtml(n.saturation_reason) + "</span></div>" : "");
    tooltip.style.opacity = 1;
    positionTooltip(cx, cy);
  }
  function positionTooltip(cx, cy) {
    var wrapRect = wrap.getBoundingClientRect();
    tooltip.style.transform = "translate(" + (cx - wrapRect.left + 14) + "px," + (cy - wrapRect.top + 14) + "px)";
  }
  function hideTooltip() { tooltip.style.opacity = 0; tooltip.style.transform = "translate(-9999px,-9999px)"; }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- detail panel ---------------------------------------------------
  var panel = document.getElementById("panel");
  var panelBody = document.getElementById("panel-body");
  document.getElementById("panel-close").addEventListener("click", function () {
    panel.classList.remove("open"); focusedId = null; render();
  });
  function openPanel(n) {
    focusedId = n.id;
    var kw = (n.keywords || []).map(function (k) { return "<span>" + escapeHtml(k) + "</span>"; }).join("");
    panelBody.innerHTML =
      "<h2>" + escapeHtml(n.label) + "</h2>" +
      "<span class=\"pill\">" + STATUS_LABEL[n.status] + "</span>" +
      "<span class=\"pill\">depth " + n.depth + "</span>" +
      "<span class=\"pill\">" + (n.split_method === "graph_cluster" ? "graph cluster" : "LLM seed") + "</span>" +
      (n.saturation_reason ? "<span class=\"pill\">stopped: " + escapeHtml(n.saturation_reason) + "</span>" : "") +
      "<div class=\"stat-row\">" +
      "<div class=\"stat\">" + n.channel_count.toLocaleString() + "<span>channels</span></div>" +
      "<div class=\"stat\">" + n.video_count.toLocaleString() + "<span>videos</span></div>" +
      (n.distinctness_score != null ? "<div class=\"stat\">" + n.distinctness_score.toFixed(2) + "<span>distinctness</span></div>" : "") +
      "</div>" +
      (kw ? "<div class=\"kw\">" + kw + "</div>" : "") +
      "<div class=\"summary\">" + (n.compaction_summary ? escapeHtml(n.compaction_summary) : "<em>No compaction summary yet.</em>") + "</div>";
    panel.classList.add("open");
    render();
  }

  // ---- search ---------------------------------------------------------
  var searchInput = document.getElementById("search");
  searchInput.addEventListener("input", function () {
    var q = searchInput.value.trim().toLowerCase();
    matchedIds.clear();
    if (q.length >= 2) {
      payload.nodes.forEach(function (n) {
        if (n.label.toLowerCase().indexOf(q) !== -1) {
          matchedIds.add(n.id);
          var p = n.parent_id;
          while (p) { collapsed.delete(p); var pn = byId.get(p); p = pn ? pn.parent_id : null; }
        }
      });
    }
    render();
  });

  // ---- expand/collapse/reset -------------------------------------------
  document.getElementById("btn-expand").addEventListener("click", function () { collapsed.clear(); render(); });
  document.getElementById("btn-collapse").addEventListener("click", function () {
    payload.nodes.forEach(function (n) { if ((payload.children[n.id] || []).length) collapsed.add(n.id); });
    payload.roots.forEach(function (rid) { collapsed.delete(rid); });
    render();
  });
  document.getElementById("btn-reset").addEventListener("click", fitToView);

  function fitToView() {
    var flat = layout();
    if (!flat.length) return;
    var minX = Math.min.apply(null, flat.map(function (e) { return e.x - BOX_W / 2; }));
    var maxX = Math.max.apply(null, flat.map(function (e) { return e.x + BOX_W / 2; }));
    var minY = Math.min.apply(null, flat.map(function (e) { return e.y - BOX_H / 2; }));
    var maxY = Math.max.apply(null, flat.map(function (e) { return e.y + BOX_H / 2; }));
    var rect = wrap.getBoundingClientRect();
    var padding = 60;
    var k = Math.min(
      (rect.width - padding * 2) / Math.max(1, maxX - minX),
      (rect.height - padding * 2) / Math.max(1, maxY - minY),
      1.4
    );
    k = Math.max(0.15, k);
    transform.k = k;
    transform.x = rect.width / 2 - ((minX + maxX) / 2) * k;
    transform.y = padding - minY * k;
    applyTransform();
  }

  // ---- table view -------------------------------------------------------
  var tableBtn = document.getElementById("btn-table");
  var tableView = document.getElementById("table-view");
  var tableBody = document.getElementById("table-body");
  var tableSort = { key: "depth", dir: 1 };
  function renderTable() {
    var rows = payload.nodes.slice().sort(function (a, b) {
      var k = tableSort.key, dir = tableSort.dir;
      var av = a[k], bv = b[k];
      if (typeof av === "string") { av = av.toLowerCase(); bv = (bv || "").toLowerCase(); }
      if (av == null) av = dir > 0 ? Infinity : -Infinity;
      if (bv == null) bv = dir > 0 ? Infinity : -Infinity;
      return av < bv ? -1 * dir : av > bv ? 1 * dir : 0;
    });
    tableBody.innerHTML = rows.map(function (n) {
      return "<tr><td>" + escapeHtml(n.label) + "</td><td class=\"n\">" + n.depth + "</td><td>" +
        STATUS_LABEL[n.status] + "</td><td class=\"n\">" + n.channel_count.toLocaleString() +
        "</td><td class=\"n\">" + n.video_count.toLocaleString() + "</td><td>" +
        (n.split_method === "graph_cluster" ? "Graph cluster" : "LLM seed") + "</td><td>" +
        (n.saturation_reason ? escapeHtml(n.saturation_reason) : "—") + "</td></tr>";
    }).join("");
  }
  document.querySelectorAll("#table-view th").forEach(function (th) {
    th.addEventListener("click", function () {
      var k = th.getAttribute("data-key");
      tableSort.dir = tableSort.key === k ? -tableSort.dir : 1;
      tableSort.key = k;
      renderTable();
    });
  });
  tableBtn.addEventListener("click", function () {
    var showing = tableBtn.getAttribute("aria-pressed") === "true";
    tableBtn.setAttribute("aria-pressed", showing ? "false" : "true");
    tableBtn.textContent = showing ? "View as table" : "View as graph";
    if (!showing) { renderTable(); tableView.style.display = "block"; }
    else { tableView.style.display = "none"; }
  });

  render();
  fitToView();
})();
</script>
</body>
</html>
"""


def _taxonomy_html(payload: dict[str, Any], manifest: dict[str, Any]) -> str:
    niche = manifest.get("niche") or "Untitled"
    n_nodes = len(payload.get("nodes", []))
    max_depth = max((n["depth"] for n in payload.get("nodes", [])), default=0)
    byline = (
        f"{n_nodes:,} nodes · depth {max_depth} · run {manifest.get('run_id', '')} · "
        f"generated {manifest.get('exported_at', '')[:10]}"
    )
    tree_json = json.dumps(payload, default=str).replace("</", "<\\/")
    html = _TAXONOMY_HTML_TEMPLATE
    html = html.replace("__TITLE__", niche)
    html = html.replace("__BYLINE__", byline)
    html = html.replace("__TREE_JSON__", tree_json)
    return html


def export_run(run_id: str, thread_id: str | None = None) -> Path:
    """Write one run's deliverable. Returns the directory."""
    from src.api import runs as runs_mod

    cfg = get_config().harness
    out = export_dir(run_id)

    if thread_id is None:
        for entry in runs_mod.load_registry():
            if entry.get("run_id") == run_id:
                thread_id = entry.get("thread_id")
                break
    if thread_id is None:
        # CLI runs are not in the registry — only console-launched ones are.
        # Their NodeLog stream carries the thread_id on every line, which is
        # how list_runs recovers them, so fall back to that rather than
        # exporting a manifest full of zeros.
        for entry in runs_mod.read_run_log(run_id):
            if entry.get("thread_id"):
                thread_id = entry["thread_id"]
                break
    if thread_id is None:
        logger.warning("export_no_thread_id", run_id=run_id)

    report: dict[str, Any] = {}
    state: dict[str, Any] = {}
    if thread_id:
        try:
            from src.api.server import _load_checkpoint

            state = _load_checkpoint(thread_id) or {}
            report = state.get("final_report") or {}
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.warning("export_checkpoint_unavailable", run_id=run_id, error=str(exc))

    channels = fetch_run_channels(run_id)
    videos = fetch_run_videos(run_id, cfg.export_max_videos)
    edges = fetch_run_edges(run_id)

    tree = state.get("tree", {}) or {}
    manifest = {
        "run_id": run_id,
        "thread_id": thread_id,
        "niche": report.get("niche") or state.get("selected_niche", ""),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": cfg.profile,
        "channels": len(channels),
        "videos": len(videos),
        "discovery_edges": len(edges),
        "attribution": _attribution_counts(channels),
        "spend": {
            "brightdata_records": state.get("brightdata_records_used", 0),
            "brightdata_usd": round(
                state.get("brightdata_records_used", 0)
                * cfg.brightdata_cost_per_record_usd, 4
            ),
            "total_usd": round(state.get("budget_spent_usd", 0.0), 4),
            "youtube_quota": state.get("youtube_quota_used", 0),
        },
        # The honest caveat. Every run so far stopped on a governor with
        # novelty still high; an export that omits this reads as a complete
        # picture of the niche when it is a survey of what was reached.
        "stop_reasons": sorted(
            {n.get("saturation_reason") for n in tree.values() if n.get("saturation_reason")}
        ),
        "saturated_branches": state.get("saturated_branches", []),
    }

    graph_payload = build_graph_payload(channels, edges, max_nodes=cfg.export_max_graph_nodes)
    tree_counts = fetch_run_tree_counts(run_id)
    taxonomy_payload = build_taxonomy_payload(tree, tree_counts)

    _write_csv(out / "channels.csv", channels)
    _write_csv(out / "outlier_videos.csv", videos)
    (out / "discovery_graph.json").write_text(
        json.dumps(graph_payload, indent=2, default=str), encoding="utf-8"
    )
    (out / "discovery_graph.html").write_text(
        _interactive_graph_html(graph_payload, manifest), encoding="utf-8"
    )
    (out / "taxonomy_tree.json").write_text(
        json.dumps(taxonomy_payload, indent=2, default=str), encoding="utf-8"
    )
    (out / "taxonomy_tree.html").write_text(
        _taxonomy_html(taxonomy_payload, manifest), encoding="utf-8"
    )
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    (out / "research_bundle.json").write_text(
        json.dumps(
            {"manifest": manifest, "report": report,
             "channels": channels, "videos": videos, "edges": edges},
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    if report:
        (out / "report.md").write_text(
            _report_markdown(report, manifest, channels), encoding="utf-8"
        )

    logger.info(
        "run_exported", run_id=run_id, path=str(out),
        channels=len(channels), videos=len(videos), edges=len(edges),
    )
    return out


def _attribution_counts(channels: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ch in channels:
        key = ch.get("discovery_method") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> None:
    import argparse

    from src.observability.logging_config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Export a run's research output")
    parser.add_argument("run_id")
    parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()

    path = export_run(args.run_id, args.thread_id)
    print(f"Exported to {path}")
    for f in sorted(path.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
