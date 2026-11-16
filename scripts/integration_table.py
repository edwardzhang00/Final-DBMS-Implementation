#!/usr/bin/env python3
"""Rebuild the workload table in REPORT_INTEGRATION.md from two run_workload.py --json files.

    python scripts/run_workload.py ... --frames 32 --json results32.json
    python scripts/run_workload.py ... --frames 4  --json results4.json
    python scripts/integration_table.py results32.json results4.json --machine "..." [--write]

Without --write it prints the new machine line, the table and the q05 timing sentence. With
--write it puts them into REPORT_INTEGRATION.md in place of the old ones. Runs made without
--pg have no PostgreSQL comparison, so the last column then says "not checked".
"""

import argparse
import json
import os
import re
import sys

REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "REPORT_INTEGRATION.md")
HEADER = ("| query | rows | B = 32: page I/O | time (s) | root rows est. / actual | peak frames "
          "| B = 4: page I/O | time (s) | root rows est. / actual | peak frames "
          "| same as PostgreSQL (B = 32 / 4) |\n|---|---|---|---|---|---|---|---|---|---|---|")


def load(path):
    with open(path, encoding="utf-8") as f:
        summary = json.load(f)
    failed = [q for q in summary["queries"] if "error" in q]
    if failed:
        sys.exit(f"{path}: these queries failed, fix them first: {', '.join(q['query'] for q in failed)}")
    return summary["frames"], {q["query"]: q for q in summary["queries"]}


def side(q):
    return (f"{q['io']} ({q['reads']} r, {q['writes']} w) | {q['seconds']:.3f} | "
            f"{q['est_root']} / {q['rows']} | {q['peak_frames']}")


def pg(q):
    if "matches_postgres" not in q:
        return "not checked"
    return "yes" if q["matches_postgres"] else "**no**"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("big", help="run_workload --json file at B = 32")
    parser.add_argument("small", help="run_workload --json file at B = 4")
    parser.add_argument("--machine", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    big_frames, big = load(args.big)
    small_frames, small = load(args.small)
    if (big_frames, small_frames) != (32, 4):
        sys.exit(f"expected runs at B = 32 and B = 4, got {big_frames} and {small_frames}")
    if sorted(big) != sorted(small):
        sys.exit("the two runs have different queries")

    rows = [f"| {name} | {big[name]['rows']} | {side(big[name])} | {side(small[name])} "
            f"| {pg(big[name])} / {pg(small[name])} |" for name in sorted(big)]
    table = HEADER + "\n" + "\n".join(rows)
    machine = (f"Machine: {args.machine}. Times are wall-clock seconds of execution (preparing\n"
               "the plan's operators and running them); optimization is done before the clock starts.")
    q32, q4 = big["q05"], small["q05"]
    ratio = q32["seconds"] / max(q4["seconds"], 0.001)
    q05 = (f"q05 does {q32['io']} page reads at B = 32 but takes {q32['seconds']:.1f} s",
           f"runs {ratio:.0f} times faster")

    if not args.write:
        print(machine + "\n\n" + table + "\n")
        print(f"q05 sentence: '{q05[0]}' ... '{q05[1]}'")
        return

    with open(REPORT, encoding="utf-8") as f:
        text = f.read()
    new, n1 = re.subn(r"Machine: .*?optimization is done before the clock starts\.", lambda m: machine,
                      text, count=1, flags=re.S)
    new, n2 = re.subn(r"\| query \| rows \| B = 32.*?(?=\n\n)", lambda m: table, new, count=1, flags=re.S)
    new, n3 = re.subn(r"q05 does \d+ page reads at B = 32 but takes [\d.]+ s", lambda m: q05[0], new, count=1)
    new, n4 = re.subn(r"runs \d+ times faster", lambda m: q05[1], new, count=1)
    if (n1, n2, n3, n4) != (1, 1, 1, 1):
        sys.exit("REPORT_INTEGRATION.md no longer has the expected layout; nothing was written")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(new)
    print("REPORT_INTEGRATION.md updated: machine line, workload table, q05 timing sentence.")
    if not all("matches_postgres" in q for q in list(big.values()) + list(small.values())):
        print("These runs had no --pg, so the PostgreSQL column says 'not checked'. The sentence "
              "'Every result equals PostgreSQL's' below the table then needs changing by hand.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    main()
