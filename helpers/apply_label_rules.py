#!/usr/bin/env python3
"""
apply_label_rules.py - fill in the 'label' column of the summary TSV using our study's
own rules (this replaces the old heuristic.csv / heuristic_7T.csv).

nii_init_gpt5.py labels most series automatically (rest/task, T1w, T2w, MP2RAGE, fmaps ...).
Our 7T protocol uses series names it does not recognise (e.g. 'mbep2d_bold', 'REV'), so
label_rules.csv tells this script what to call them.

Only rows whose label is still EMPTY are touched - an automatic label is never overwritten.
SBRef and 'setter' series are always skipped.

Usage: python apply_label_rules.py summaries/sub-X_ses-01.tsv label_rules.csv 7T
"""
import csv
import sys

import pandas as pd

tsv, rules_csv, magnet = sys.argv[1], sys.argv[2], sys.argv[3].upper()

df = pd.read_csv(tsv, sep="\t", dtype=str).fillna("")
if "SeriesDescription" not in df.columns or "label" not in df.columns:
    sys.exit(f"ERROR: {tsv} has no SeriesDescription/label columns - is it a nii_init TSV?")

with open(rules_csv, newline="") as fh:
    rules = [r for r in csv.DictReader(fh)
             if r["magnet"].upper() in ("ANY", magnet)]

desc = df["SeriesDescription"]
skip = desc.str.contains("SBRef|setter", case=False, regex=True)
changed = 0
for r in rules:
    hit = desc.str.contains(r["lookfor"], case=True, regex=False) & (df["label"] == "") & ~skip
    if hit.any():
        df.loc[hit, "label"] = r["label"]
        changed += int(hit.sum())
        print(f"rule '{r['lookfor']}' -> {r['label']}: {int(hit.sum())} row(s)")
        for d in sorted(set(desc[hit])):
            print(f"     {d}")

df.to_csv(tsv, sep="\t", index=False)
print(f"{changed} row(s) labelled by label_rules.csv ({magnet}); TSV saved: {tsv}")
