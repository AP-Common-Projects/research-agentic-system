"""Cross-model LLM-as-judge for synthesized research findings.

Why this exists
---------------
Production grades findings deterministically (``src.nodes.synthesize.grade_findings``)
using the *frontier* model family (DeepSeek). That deterministic grade can be gamed
by a model that learns to over-cite channels — so we need an *independent* second
opinion from a *different* model family. The ``cross_judge`` tier routes to
kimi-k3 (see ``src.llm.cascade.TIER_MAP``), so a DeepSeek-produced finding is judged
by a Kimi model. **Never let a model grade its own output** — that is the entire
reason ``cross_judge`` is a separate tier from ``frontier``/``mid``.

Calibration protocol
--------------------
Before trusting this judge at scale, calibrate it against 20-30 hand-graded cases::

    python -m src.eval.judge --calibrate path/to/hand_graded.jsonl

The hand-graded file is JSONL, one object per line:

    {"case_id": "h001", "claim": "...", "expected_grade": "strong|moderate|weak",
     "reason": "human's one-line justification"}

``calibrate`` runs the judge over every case, compares judge vs human, and reports
agreement rate plus a per-case disagreement table. Treat agreement rate below ~80%
as a signal to tighten the rubric criteria (below), not as a reason to ship anyway.
Re-run calibration any time you change the rubric, the judge model, or the
production grading thresholds.

Rubric (explicit, checkable criteria — NOT an open-ended 1-10 score)
--------------------------------------------------------------------
C1 Corroboration  : >=3 distinct supporting channels = strong; 2 = moderate; else weak.
C2 Effect size    : max outlier score >= 3.0 = strong; >= 2.0 = moderate; else weak.
C3 Recency        : >=50% evidence within 90 days = strong; >=50% within 365 days = moderate; else weak.
C4 Consistency    : coefficient of variation (sample) < 0.5 = strong; < 1.5 = moderate; else weak.
C5 Claim fidelity : the claim must be directly supported by the cited evidence, must not
                    assert causality from correlational data, and must not overstate the
                    magnitude of the observed effect. (This is the only axis the LLM
                    assesses — the structural C1-C4 axes are computed deterministically.)

Final grade
-----------
    strong   if C1 == strong AND >=2 of {C2,C3,C4} == strong AND none weak AND C5 passes.
    moderate if C1 in {strong, moderate} AND not all of {C2,C3,C4} weak AND C5 passes.
    weak     otherwise (including any C5 failure).
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from src.llm.json_parse import JSONResponseError, loads_forgiving
from src.llm.cascade import complete_tier, get_model_for_tier

JUDGE_TIER = "cross_judge"

SYSTEM_PROMPT = """You are an independent research-grading judge. You grade a single \
synthesized finding for CLAIM FIDELITY only. You never grade corroboration counts, effect \
sizes, recency, or consistency yourself — those are computed deterministically from structured data.

Checklist (answer each with yes/no):
1. Is the claim directly supported by the evidence cited (channel ids / outlier scores / pattern)?
2. Does the claim assert causality ("causes", "because") from what is only correlational data? \
(flag as overreach if yes)
3. Does the claim overstate the magnitude (e.g. "massive", "guaranteed", "always")?

Respond with ONLY a JSON object, no prose, no markdown fences:
{
  "supported": true,
  "overreach": false,
  "rationale": "<one sentence>"
}
"""


def _structural_axes(finding: dict[str, Any]) -> dict[str, str]:
    """Deterministically score C1-C4. Falls back to precomputed evidence axes when present."""
    ev = finding.get("evidence") or {}

    supporting = finding.get("supporting_channel_ids") or []
    n = len(set(supporting))
    if n >= 3:
        corroboration = "strong"
    elif n == 2:
        corroboration = "moderate"
    else:
        corroboration = "weak"

    # Prefer precomputed axes (e.g. from grade_findings output), else recompute from scores.
    scores = ev.get("outlier_scores")
    if scores is None and isinstance(ev.get("max_outlier_score"), (int, float)):
        scores = [ev["max_outlier_score"]]
    scores = [float(s) for s in (scores or []) if isinstance(s, (int, float)) and s > 0]

    effect_size = ev.get("effect_size")
    if effect_size is None and scores:
        mx = max(scores)
        effect_size = "strong" if mx >= 3.0 else "moderate" if mx >= 2.0 else "weak"
    effect_size = effect_size or "weak"

    consistency = ev.get("consistency")
    if consistency is None and len(scores) >= 2:
        try:
            mean = statistics.mean(scores)
            var = statistics.variance(scores)
            cv = var / mean if mean > 0 else float("inf")
            consistency = "strong" if cv < 0.5 else "moderate" if cv < 1.5 else "weak"
        except Exception:
            consistency = "weak"
    consistency = consistency or "weak"

    recency = ev.get("recency") or "weak"

    return {
        "corroboration": corroboration,
        "effect_size": effect_size,
        "recency": recency,
        "consistency": consistency,
    }


def _final_grade(axes: dict[str, str], claim_ok: bool) -> str:
    if not claim_ok:
        return "weak"
    other = [axes["effect_size"], axes["recency"], axes["consistency"]]
    strong_others = sum(1 for a in other if a == "strong")
    weak_others = sum(1 for a in other if a == "weak")

    if axes["corroboration"] == "strong" and strong_others >= 2 and weak_others == 0:
        return "strong"
    if axes["corroboration"] in ("strong", "moderate") and weak_others != len(other):
        return "moderate"
    return "weak"


def _judge_one(finding: dict[str, Any]) -> dict[str, Any]:
    """Run the cross-model claim-fidelity check for a single finding (makes a real LLM call)."""
    assert get_model_for_tier(JUDGE_TIER)["provider"] != "deepseek", (
        "cross_judge tier must use a different provider than production; refusing to "
        "let a model grade its own output."
    )

    axes = _structural_axes(finding)
    prompt = (
        f"Finding claim: {finding.get('claim', '')}\n"
        f"Supporting channel ids: {finding.get('supporting_channel_ids', [])}\n"
        f"Pattern type: {finding.get('pattern_type', '')}\n"
        f"Structural evidence axes: {json.dumps(axes)}\n"
        f"Evidence payload: {json.dumps(finding.get('evidence') or {}, default=str)}\n"
        "Grade this finding for claim fidelity per the checklist."
    )

    result = complete_tier(JUDGE_TIER, prompt, SYSTEM_PROMPT)
    content = result.get("content", "")
    try:
        parsed = loads_forgiving(content, expect="object")
    except JSONResponseError:
        parsed = {}

    supported = bool(parsed.get("supported", False))
    overreach = bool(parsed.get("overreach", False))
    claim_ok = supported and not overreach

    return {
        "grade": _final_grade(axes, claim_ok),
        "claim_supported": supported,
        "overreach": overreach,
        "rationale": parsed.get("rationale", ""),
        "axes": axes,
        "judge_model": get_model_for_tier(JUDGE_TIER)["model_name"],
    }


def judge_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the rubric to a list of findings. Returns one judged dict per finding.

    Each input finding is a dict with at least ``claim``; ``supporting_channel_ids``,
    ``pattern_type`` and ``evidence`` (optionally precomputed axes from grade_findings)
    are used to score the structural C1-C4 criteria deterministically.
    """
    return [_judge_one(f) for f in findings]


def _load_hand_graded(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def calibrate(hand_graded_file: str) -> dict[str, Any]:
    """Compare the judge against hand-graded cases; report agreement and disagreements.

    Reads a JSONL (or JSON array) of ``{case_id, claim, expected_grade, reason}``,
    runs the judge on each, and returns a report dict with ``agreement_rate``,
    ``n_cases``, and a ``disagreements`` list.
    """
    hand_graded = _load_hand_graded(hand_graded_file)

    judged = judge_findings(
        [{"claim": h["claim"], "supporting_channel_ids": h.get("supporting_channel_ids", [])}
         for h in hand_graded]
    )

    disagreements: list[dict[str, Any]] = []
    for h, j in zip(hand_graded, judged):
        if j["grade"] != h["expected_grade"]:
            disagreements.append({
                "case_id": h.get("case_id", ""),
                "claim": h["claim"],
                "expected": h["expected_grade"],
                "judged": j["grade"],
                "rationale": j["rationale"],
                "human_reason": h.get("reason", ""),
            })

    n = len(hand_graded)
    agreement = (n - len(disagreements)) / n if n else 0.0

    return {
        "n_cases": n,
        "agreement_rate": round(agreement, 4),
        "disagreements": disagreements,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-model LLM judge for synthesized findings.")
    parser.add_argument(
        "--calibrate", metavar="PATH",
        help="Run judge against a hand-graded JSONL batch and report agreement rate.",
    )
    parser.add_argument(
        "--findings", metavar="PATH",
        help="Judge a JSON file of findings (list of dicts) and print graded results.",
    )
    args = parser.parse_args(argv)

    if args.calibrate:
        report = calibrate(args.calibrate)
        print(json.dumps(report, indent=2))
        return 0

    if args.findings:
        findings = _load_hand_graded(args.findings)
        for judged in judge_findings(findings):
            print(json.dumps(judged, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
