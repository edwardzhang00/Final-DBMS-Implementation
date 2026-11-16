#!/usr/bin/env python3
"""Generate R(a, b) and S(b, c) for the performance study, at a controlled
match rate.

The match rate k is the (expected) number of S tuples that share their b
value with any given R tuple.  We draw b from {0, ..., d-1} with
d = max(1, m // k); then an R tuple's b matches m / d ~= k tuples of S.

Usage:
    python datagen.py --n 1000 --m 1000 --match-rate 10 --out data/prog_1000.txt
"""
import argparse
import os
import random


def generate(n, m, match_rate, seed=0):
    rng = random.Random(seed)
    k = max(1, match_rate)
    b_domain_size = max(1, m // k)
    # a is unique (R stays a set even after the join), b is drawn from the
    # small shared domain.
    R = [(i, rng.randrange(b_domain_size)) for i in range(n)]
    S = [(rng.randrange(b_domain_size), j) for j in range(m)]
    return R, S, b_domain_size


def write_program(R, S, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write("R (a, b) = {\n")
        for a, b in R:
            f.write(f"  {a}, {b}\n")
        f.write("}\n\n")
        f.write("S (b, c) = {\n")
        for b, c in S:
            f.write(f"  {b}, {c}\n")
        f.write("}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--m", type=int, required=True)
    parser.add_argument("--match-rate", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    R, S, b_domain_size = generate(args.n, args.m, args.match_rate, args.seed)
    write_program(R, S, args.out)
    print(f"wrote {args.out}: |R|={len(R)} |S|={len(S)} b-domain={b_domain_size} "
          f"(expected match rate {len(S)/b_domain_size:.2f})")


if __name__ == "__main__":
    main()