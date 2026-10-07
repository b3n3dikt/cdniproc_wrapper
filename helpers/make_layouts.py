#!/usr/bin/env python3
"""
make_layouts.py  -  build the two final BIDS datasets from derivatives/nordic

Input  (one subject, after NORDIC + post-NORDIC cleanup):
    derivatives/nordic/sub-X/ses-01/{func,fmap,anat}
    derivatives/nordic/sub-X/ses-02/{func,fmap,anat}

Output 1  bids_combined/   every session merged into ONE session (for fMRIPrep)
    sub-X/ses-combined/{func,fmap,anat}     runs renumbered 01, 02, 03 ...

Output 2  bids_sessions/   sessions kept separate
    sub-X/anat/                              anatomicals outside the sessions
    sub-X/ses-01/{func,fmap}
    sub-X/ses-02/{func,fmap}
    (use --anat-in-session to keep anat inside each ses-* folder instead)

Only what fMRIPrep needs is copied: func *_bold, fmap *_epi, anat T1w/T2w.
Anything else (MP2RAGE parts, UNIT1, dwi ...) stays behind in derivatives/nordic.

Every JSON sidecar gets OriginalSession / OriginalRun / OriginalFilename so you can
trace a file back to its source. Any old IntendedFor is removed from fmap JSONs
(paths change, so IntendedFor must be regenerated - run_intendedfor does that).

Usage:
    python make_layouts.py --nordic-root $ROOT/derivatives/nordic --sub SUB001 \
        --out-combined $ROOT/bids_combined --out-sessions $ROOT/bids_sessions
    add --dry-run to print the old-name -> new-name plan without copying anything.
"""
import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# BIDS entity order, used only to decide where a missing run-XX goes in a filename
ENTITY_ORDER = ["sub", "ses", "sample", "task", "acq", "ce", "trc", "rec", "dir",
                "run", "mod", "echo", "flip", "inv", "mt", "part", "proc", "chunk"]
EXTS = (".nii.gz", ".nii", ".json", ".bval", ".bvec")
# datatype -> suffixes we carry over
KEEP = {"func": {"bold"}, "fmap": {"epi"}, "anat": {"T1w", "T2w"}}


def split_name(name):
    """'sub-X_ses-01_run-1_T1w.nii.gz' -> ([('sub','X'),('ses','01'),('run','1')], 'T1w', '.nii.gz')"""
    for ext in EXTS:
        if name.endswith(ext):
            stem = name[: -len(ext)]
            break
    else:
        return None
    parts = stem.split("_")
    ents = []
    for p in parts[:-1]:
        k, _, v = p.partition("-")
        ents.append((k, v))
    return ents, parts[-1], ext


def join_name(ents, suffix, ext):
    return "_".join([f"{k}-{v}" for k, v in ents] + [suffix]) + ext


def get(ents, key):
    for k, v in ents:
        if k == key:
            return v
    return None


def set_entity(ents, key, val):
    """Replace entity if present, otherwise insert it where BIDS says it belongs."""
    ents = list(ents)
    for i, (k, _) in enumerate(ents):
        if k == key:
            ents[i] = (key, val)
            return ents
    rank = ENTITY_ORDER.index(key)
    for i, (k, _) in enumerate(ents):
        if k in ENTITY_ORDER and ENTITY_ORDER.index(k) > rank:
            ents.insert(i, (key, val))
            return ents
    ents.append((key, val))
    return ents


def drop_entity(ents, key):
    return [(k, v) for k, v in ents if k != key]


def run_sort_key(run):
    return (0, int(run)) if run and run.isdigit() else (1, run or "")


def collect(nordic_sub, sessions, datatypes):
    """Return list of dicts describing every file we will carry over."""
    items = []
    skipped = defaultdict(int)
    for ses_dir in sessions:
        ses = ses_dir.name[len("ses-"):]
        for dt in datatypes:
            dt_dir = ses_dir / dt
            if not dt_dir.is_dir():
                continue
            for f in sorted(dt_dir.iterdir()):
                if not f.is_file():
                    continue
                parsed = split_name(f.name)
                if parsed is None:
                    continue
                ents, suffix, ext = parsed
                if suffix not in KEEP[dt]:
                    skipped[f"{dt}/{suffix}"] += 1
                    continue
                items.append(dict(path=f, dt=dt, ses=ses, ents=ents,
                                  suffix=suffix, ext=ext,
                                  run=get(ents, "run") or "01"))
    return items, skipped


def assign_new_runs(items):
    """
    New run number per 'series' (same datatype/task/acq/dir/suffix/part, any echo),
    counted across sessions in session order. Echoes and .nii/.json of the same
    original run share one new run number.
    """
    groups = defaultdict(set)
    for it in items:
        series = (it["dt"], it["suffix"],
                  tuple((k, v) for k, v in it["ents"] if k not in ("sub", "ses", "run", "echo")))
        it["series"] = series
        groups[series].add((it["ses"], it["run"]))
    new_run = {}
    for series, runs in groups.items():
        ordered = sorted(runs, key=lambda sr: (sr[0], run_sort_key(sr[1])))
        for n, sr in enumerate(ordered, start=1):
            new_run[(series, sr)] = f"{n:02d}"
    for it in items:
        it["new_run"] = new_run[(it["series"], (it["ses"], it["run"]))]


def write_item(it, dest, dry_run, tag_new_run):
    print(f"  ses-{it['ses']}/{it['dt']}/{it['path'].name}\n      -> {dest.name}")
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if it["ext"] != ".json":
        shutil.copy2(it["path"], dest)
        return
    with open(it["path"]) as fh:
        meta = json.load(fh)
    meta.pop("IntendedFor", None)
    meta["OriginalSession"] = f"ses-{it['ses']}"
    meta["OriginalRun"] = f"run-{it['run']}"
    meta["OriginalFilename"] = it["path"].name
    if tag_new_run:
        meta["NewRun"] = f"run-{it['new_run']}"
    with open(dest, "w") as fh:
        json.dump(meta, fh, indent=4)
        fh.write("\n")


def build_combined(items, sub, out_root, dry_run, map_rows):
    print(f"\n== bids_combined: {out_root}/sub-{sub}/ses-combined")
    for it in sorted(items, key=lambda i: (i["dt"], i["series"], i["new_run"], i["ses"], i["path"].name)):
        ents = set_entity(set_entity(it["ents"], "ses", "combined"), "run", it["new_run"])
        name = join_name(ents, it["suffix"], it["ext"])
        dest = out_root / f"sub-{sub}" / "ses-combined" / it["dt"] / name
        write_item(it, dest, dry_run, tag_new_run=True)
        map_rows.append(("combined", f"ses-{it['ses']}/{it['dt']}/{it['path'].name}", str(dest.relative_to(out_root))))


def build_sessions(items, sub, out_root, anat_in_session, dry_run, map_rows):
    print(f"\n== bids_sessions: {out_root}/sub-{sub}  (anat {'inside sessions' if anat_in_session else 'at subject level'})")
    for it in sorted(items, key=lambda i: (i["dt"], i["ses"], i["path"].name)):
        if it["dt"] == "anat" and not anat_in_session:
            ents = drop_entity(it["ents"], "ses")
            ents = set_entity(ents, "run", it["new_run"])
            name = join_name(ents, it["suffix"], it["ext"])
            dest = out_root / f"sub-{sub}" / "anat" / name
            tag = True
        else:
            name = it["path"].name
            dest = out_root / f"sub-{sub}" / f"ses-{it['ses']}" / it["dt"] / name
            tag = False
        write_item(it, dest, dry_run, tag_new_run=tag)
        map_rows.append(("sessions", f"ses-{it['ses']}/{it['dt']}/{it['path'].name}", str(dest.relative_to(out_root))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nordic-root", required=True, type=Path, help="derivatives/nordic (contains sub-*/)")
    ap.add_argument("--sub", required=True, help="subject label, with or without 'sub-'")
    ap.add_argument("--out-combined", required=True, type=Path)
    ap.add_argument("--out-sessions", required=True, type=Path)
    ap.add_argument("--sessions", nargs="*", help="only these sessions (labels with or without 'ses-'); default all")
    ap.add_argument("--anat-in-session", action="store_true",
                    help="bids_sessions: keep anat inside each ses-* folder instead of sub-X/anat")
    ap.add_argument("--dataset-description", type=Path, help="copied into both output roots if missing")
    ap.add_argument("--map-out", type=Path, help="write a TSV of old-path -> new-path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sub = args.sub[4:] if args.sub.startswith("sub-") else args.sub
    nordic_sub = args.nordic_root / f"sub-{sub}"
    if not nordic_sub.is_dir():
        sys.exit(f"ERROR: {nordic_sub} not found")

    sessions = sorted(p for p in nordic_sub.glob("ses-*") if p.is_dir() and p.name != "ses-combined")
    if args.sessions:
        want = {s if s.startswith("ses-") else f"ses-{s}" for s in args.sessions}
        sessions = [p for p in sessions if p.name in want]
    if not sessions:
        sys.exit(f"ERROR: no sessions found under {nordic_sub}")
    print("sessions:", ", ".join(p.name for p in sessions))

    items, skipped = collect(nordic_sub, sessions, list(KEEP))
    if not any(i["dt"] == "func" for i in items):
        sys.exit("ERROR: no func *_bold files found - did NORDIC / post-NORDIC finish?")
    for dt in ("anat", "fmap"):
        if not any(i["dt"] == dt for i in items):
            print(f"WARNING: no {dt} files found for sub-{sub}", file=sys.stderr)
    assign_new_runs(items)

    if not args.dry_run:
        for root in (args.out_combined, args.out_sessions):
            target = root / f"sub-{sub}"
            if target.exists():
                print(f"removing old {target}")
                shutil.rmtree(target)

    map_rows = []
    build_combined(items, sub, args.out_combined, args.dry_run, map_rows)
    build_sessions(items, sub, args.out_sessions, args.anat_in_session, args.dry_run, map_rows)

    if skipped:
        print("\nnot copied (not needed by fMRIPrep): " +
              ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())))

    if not args.dry_run:
        if args.dataset_description and args.dataset_description.exists():
            for root in (args.out_combined, args.out_sessions):
                dd = root / "dataset_description.json"
                if not dd.exists():
                    shutil.copy2(args.dataset_description, dd)
        if args.map_out:
            args.map_out.parent.mkdir(parents=True, exist_ok=True)
            with open(args.map_out, "w") as fh:
                fh.write("layout\tsource (derivatives/nordic/sub-X)\tdestination\n")
                for row in map_rows:
                    fh.write("\t".join(row) + "\n")
            print(f"\nfile map written to {args.map_out}")


if __name__ == "__main__":
    main()
