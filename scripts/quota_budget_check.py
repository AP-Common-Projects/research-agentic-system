#!/usr/bin/env python3
"""Quota budget check — deterministic pre-flight math for external API quotas."""
import json
import sys
import argparse

EXAMPLE_CONFIG = {
    "resources": [
        {
            "name": "YouTube Data API v3",
            "period": "daily",
            "ceiling": 10000,
            "warn_threshold_pct": 90,
            "operations": [
                {"name": "search.list", "unit_cost": 100, "planned_calls": 0},
                {"name": "channels.list", "unit_cost": 1, "planned_calls": 0},
                {"name": "videos.list", "unit_cost": 1, "planned_calls": 0},
                {"name": "commentThreads.list", "unit_cost": 1, "planned_calls": 0},
            ],
        },
        {
            "name": "Bright Data concurrent requests",
            "period": "instantaneous",
            "ceiling": 20,
            "warn_threshold_pct": 80,
            "operations": [
                {"name": "scraper requests", "unit_cost": 1, "planned_calls": 5},
            ],
        },
        {
            "name": "DeepSeek V4-Pro tokens",
            "period": "per-run",
            "ceiling": 10000000,
            "warn_threshold_pct": 80,
            "operations": [
                {"name": "prompt_tokens", "unit_cost": 1, "planned_calls": 5000000},
                {"name": "completion_tokens", "unit_cost": 1, "planned_calls": 5000000},
            ],
        },
    ]
}


def check_resource(resource):
    total = sum(op["unit_cost"] * op["planned_calls"] for op in resource["operations"])
    ceiling = resource["ceiling"]
    warn_pct = resource.get("warn_threshold_pct", 80)
    pct_used = (total / ceiling * 100) if ceiling else float("inf")

    if pct_used >= 100:
        status = "EXCEEDS"
    elif pct_used >= warn_pct:
        status = "WARN"
    else:
        status = "OK"

    return {
        "name": resource["name"],
        "period": resource.get("period", "unspecified"),
        "total": total,
        "ceiling": ceiling,
        "pct_used": pct_used,
        "status": status,
    }


def format_report(results):
    lines = []
    lines.append(f"{'Resource':<30} {'Period':<16} {'Planned':>10} {'Ceiling':>10} {'% Used':>8}  Status")
    lines.append("-" * 90)
    for r in results:
        lines.append(
            f"{r['name']:<30} {r['period']:<16} {r['total']:>10} {r['ceiling']:>10} "
            f"{r['pct_used']:>7.1f}%  {r['status']}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Pre-flight quota budget check.")
    parser.add_argument("config", nargs="?", help="Path to a JSON config file.")
    parser.add_argument("--write-example", metavar="PATH", help="Write an example config to PATH and exit.")
    args = parser.parse_args()

    if args.write_example:
        with open(args.write_example, "w") as f:
            json.dump(EXAMPLE_CONFIG, f, indent=2)
        print(f"Wrote example config to {args.write_example}")
        return 0

    if not args.config:
        parser.print_help()
        return 1

    with open(args.config) as f:
        config = json.load(f)

    results = [check_resource(r) for r in config["resources"]]
    print(format_report(results))

    any_warn_or_exceed = any(r["status"] != "OK" for r in results)
    if any_warn_or_exceed:
        print("\nAt least one resource is at or above its warning threshold — "
              "review before starting an unattended run.")
        return 1

    print("\nAll resources within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())