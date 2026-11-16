#!/usr/bin/env python3
"""Generate the workload data set as CSV files (no header), deterministically.

    python workload/generate.py OUT_DIR [--scale 1.0]

Two pairs of columns are strongly correlated on purpose:
  emp.level and emp.salary   salary = 30000 + 15000 * level + up to 14999, so a
                             salary band picks out exactly one level
  dept.city and dept.region  every city lies in one region
emp.did is skewed (a third of employees work in ten departments) and an
employee usually lives in the city of their department.
"""
import argparse
import os
import random

REGIONS = {
    "East": ["Ottawa", "Montreal", "Quebec", "Halifax"],
    "West": ["Vancouver", "Victoria", "Calgary", "Edmonton"],
    "Central": ["Toronto", "Hamilton", "London", "Kingston"],
    "Prairie": ["Winnipeg", "Regina", "Saskatoon", "Brandon"],
    "North": ["Whitehorse", "Yellowknife", "Iqaluit", "Inuvik"],
}
CITIES = [c for cities in REGIONS.values() for c in cities]
REGION_OF = {c: r for r, cities in REGIONS.items() for c in cities}


def generate(out, scale=1.0, seed=2026):
    rng = random.Random(seed)
    n_dept, n_emp, n_proj = 100, int(10000 * scale), int(1000 * scale)
    os.makedirs(out, exist_ok=True)

    dept_city = {}
    with open(os.path.join(out, "dept.csv"), "w") as f:
        for did in range(n_dept):
            city = CITIES[did % len(CITIES)]
            dept_city[did] = city
            f.write(f"{did},Dept-{did:03d},{rng.randrange(1000, 100001)},{city},{REGION_OF[city]}\n")

    with open(os.path.join(out, "emp.csv"), "w") as f:
        for eid in range(n_emp):
            did = rng.randrange(10) if rng.random() < 1 / 3 else rng.randrange(n_dept)
            level = rng.randrange(1, 6)
            salary = 30000 + 15000 * level + rng.randrange(15000)
            city = dept_city[did] if rng.random() < 0.8 else rng.choice(CITIES)
            mgr = rng.randrange(n_emp)
            f.write(f"{eid},emp{eid:05d},{rng.randrange(20, 66)},{did},{salary},{level},{city},{mgr}\n")

    with open(os.path.join(out, "project.csv"), "w") as f:
        for pid in range(n_proj):
            f.write(f"{pid},{rng.randrange(n_dept)},Proj-{pid:04d},{rng.randrange(10000, 1000001)}\n")

    with open(os.path.join(out, "works.csv"), "w") as f:
        for eid in range(n_emp):
            for pid in rng.sample(range(n_proj), 3):
                f.write(f"{eid},{pid},{rng.randrange(1, 41)}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    generate(args.out, args.scale)
