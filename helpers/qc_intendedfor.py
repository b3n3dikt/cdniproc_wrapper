#!/usr/bin/env python3
"""
qc_intendedfor.py - sanity check of the fieldmap <-> functional-run mapping (IntendedFor) of one BIDS layout.

Run it after IntendedFor has been written (step 04 does this for every layout it builds). It reads what is actually
on disk, also asks IntendedFor_JSBR.py (dry run, nothing written) what each --assign-mode would have chosen, and writes

    <out-dir>/intendedfor_sub-<SUB>_<layout>.tsv     one row per functional run
    <out-dir>/bids_setup_sub-<SUB>_<layout>.md       readable report (status, FAIL/WARN list, tables)
    <out-dir>/bids_setup_summary.tsv                 one row per (subject, layout); rows are replaced on rerun

Exit code: 0 = OK or WARN only, 2 = at least one FAIL.

FAIL  a run has fieldmaps available but none is mapped to it
      a run has no fieldmap in its session and SyN-SDC is not enabled (fMRIPrep would run without distortion correction)
      a run has no fieldmap with the opposite phase-encoding direction
      a fieldmap is mapped to a run from a different original session
WARN  a run has no fieldmap in its session but SyN-SDC is enabled (SyN-SDC only)
      a fieldmap pair is not used by any run
      the chosen mode disagrees with most of the other modes for a run
      fieldmap-to-run gap above --max-gap-min, or fieldmap acquired after the run
      ShimSetting / TotalReadoutTime differs between a run and its fieldmap
      B0FieldIdentifier/B0FieldSource present (SDCFlows then ignores IntendedFor)
      no AcquisitionDate in the sidecars (times are time-of-day only)

Usage:
    python qc_intendedfor.py <bids_root> --sub SUB001 --out-dir <ROOT>/qc [--mode lookforward] [--syn-sdc] [--max-gap-min 30]
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from IntendedFor_JSBR import get_timestamp, shim_key  # noqa: E402

MODES = ["auto", "nearest", "lookback", "lookforward"]
EXT = re.compile(r"\.(nii\.gz|nii|json)$")


def read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {}


def run_key(name):
    n = EXT.sub("", name.split("/")[-1])
    return re.sub(r"_part-(mag|phase)", "", re.sub(r"_echo-\d+", "", n))


def pair_key(name):
    return re.sub(r"_dir-[A-Za-z]+", "", EXT.sub("", name.split("/")[-1]))


def opposite(pe):
    if not pe:
        return None
    return pe[:-1] if pe.endswith("-") else pe + "-"


def jsbr_mapping(bids_root, sub, ses, mode):
    """Dry-run IntendedFor_JSBR.py and return {run_key: {pair_key, ...}} (or None if it failed)."""
    cmd = [sys.executable, str(HERE / "IntendedFor_JSBR.py"), str(bids_root), "--sub", f"sub-{sub}",
           "--ses", ses, "--assign-mode", mode]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True).stdout
    except Exception:
        return None
    mapping, cur = defaultdict(set), None
    for line in out.splitlines():
        if line.startswith("[fmap]"):
            cur = pair_key(line.split()[-1])
        elif line.strip().startswith("- ") and cur:
            mapping[run_key(line.strip()[2:])].add(cur)
    return mapping


def load_session(ses_dir):
    funcs = {}
    for j in sorted((ses_dir / "func").glob("*_bold.json")):
        if "part-phase" in j.name:
            continue
        m = read_json(j)
        k = run_key(j.name)
        t = get_timestamp(m, j)
        e = funcs.get(k)
        if e is None or (t is not None and (e["time"] is None or t < e["time"])):
            funcs[k] = {"meta": m, "time": t, "json": j}
    fmaps = {}
    for j in sorted((ses_dir / "fmap").glob("*_epi.json")):
        m = read_json(j)
        fmaps[j.name] = {"meta": m, "time": get_timestamp(m, j), "pair": pair_key(j.name),
                         "intended": {run_key(x) for x in m.get("IntendedFor", [])}, "json": j}
    return funcs, fmaps


def orig_ses(meta, default):
    return meta.get("OriginalSession") or default


def fmt_t(t):
    return "" if t is None else f"{int(t // 3600) % 24:02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bids_root", type=Path)
    ap.add_argument("--sub", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--mode", default="auto", help="the --assign-mode that was used when IntendedFor was written")
    ap.add_argument("--syn-sdc", action="store_true", help="SyN-SDC is enabled in the fMRIPrep flags (missing fieldmaps are then a WARN)")
    ap.add_argument("--max-gap-min", type=float, default=30.0)
    a = ap.parse_args()

    sub = a.sub[4:] if a.sub.startswith("sub-") else a.sub
    layout = a.bids_root.name
    sub_dir = a.bids_root / f"sub-{sub}"
    ses_dirs = sorted(p for p in sub_dir.glob("ses-*") if p.is_dir())
    if not ses_dirs:
        sys.exit(f"ERROR: no sessions under {sub_dir}")

    rows, issues = [], []          # issues: (level, session, run, message)
    unused_pairs = 0
    n_nodate = 0
    mode_tables = []

    for sd in ses_dirs:
        funcs, fmaps = load_session(sd)
        if not funcs:
            continue
        mode_maps = {m: jsbr_mapping(a.bids_root, sub, sd.name, m) for m in MODES}
        # on-disk mapping per run (what fMRIPrep will actually see)
        disk = defaultdict(set)
        for fn, f in fmaps.items():
            for rk in f["intended"]:
                disk[rk].add(f["pair"])

        used_pairs = set()
        pairs_by_ses = defaultdict(set)
        for fn, f in fmaps.items():
            pairs_by_ses[orig_ses(f["meta"], sd.name)].add(f["pair"])
        for fn, f in fmaps.items():
            if "AcquisitionDate" not in f["meta"] and "AcquisitionDateTime" not in f["meta"]:
                n_nodate += 1
            if "B0FieldIdentifier" in f["meta"] or "B0FieldSource" in f["meta"]:
                issues.append(("WARN", sd.name, "", f"{fn}: B0FieldIdentifier/B0FieldSource present - SDCFlows then ignores IntendedFor"))

        for rk, r in sorted(funcs.items(), key=lambda kv: (kv[1]["time"] or 0, kv[0])):
            rm = r["meta"]
            rses = orig_ses(rm, sd.name)
            avail = [fn for fn, f in fmaps.items() if orig_ses(f["meta"], sd.name) == rses]
            mapped = [fn for fn, f in fmaps.items() if rk in f["intended"]]
            foreign = [fn for fn in mapped if orig_ses(fmaps[fn]["meta"], sd.name) != rses]
            flags = []

            def add(level, msg):
                flags.append(f"{level}: {msg}")
                issues.append((level, sd.name, rk, msg))

            if not avail:
                add("WARN" if a.syn_sdc else "FAIL",
                    "no fieldmap in this session" + (" - SyN-SDC only" if a.syn_sdc else " and SyN-SDC is not enabled (no distortion correction)"))
            elif not mapped:
                add("FAIL", f"{len(avail)} fieldmap file(s) available in the session but none is mapped to this run")
            if foreign:
                add("FAIL", "mapped to a fieldmap from another session: " + ", ".join(foreign))
            if mapped:
                want = opposite(rm.get("PhaseEncodingDirection"))
                pes = {fmaps[fn]["meta"].get("PhaseEncodingDirection") for fn in mapped}
                if want and want not in pes:
                    add("FAIL", f"no mapped fieldmap with the opposite phase-encoding direction ({want}); mapped: {sorted(p for p in pes if p)}")
                for fn in mapped:
                    f = fmaps[fn]
                    used_pairs.add((orig_ses(f["meta"], sd.name), f["pair"]))
                    if shim_key(f["meta"]) != shim_key(rm):
                        add("WARN", f"ShimSetting of {fn} differs from the run")
                    trf, trr = f["meta"].get("TotalReadoutTime"), rm.get("TotalReadoutTime")
                    if trf is not None and trr is not None and abs(trf - trr) > 1e-6:
                        add("WARN", f"TotalReadoutTime of {fn} differs from the run ({trf} vs {trr})")
                gaps = [(f["time"] - r["time"]) / 60.0 for fn in mapped for f in [fmaps[fn]]
                        if f["time"] is not None and r["time"] is not None]
                if gaps:
                    near = min(gaps, key=abs)
                    if near > 0:
                        add("WARN", f"closest mapped fieldmap was acquired {near:.0f} min AFTER the run")
                    elif abs(near) > a.max_gap_min:
                        add("WARN", f"closest mapped fieldmap is {abs(near):.0f} min before the run (> {a.max_gap_min:.0f})")
            # compare with the other modes
            chosen = disk.get(rk, set())
            alt = {}
            for m in MODES:
                mm = mode_maps[m]
                alt[m] = None if mm is None else sorted(mm.get(rk, set()))
            others = [m for m in MODES if m != a.mode and mode_maps[m] is not None]
            differ = [m for m in others if set(alt[m] or []) != chosen]
            if mapped and len(others) >= 2 and len(differ) * 2 > len(others):
                add("WARN", f"chosen mode '{a.mode}' disagrees with: {', '.join(differ)}")
            rows.append({
                "layout": layout, "session": rses, "run": rk, "time": fmt_t(r["time"]),
                "phase_encoding": rm.get("PhaseEncodingDirection", ""),
                "n_fmaps_in_session": len(avail),
                "mapped_pairs": ";".join(sorted(chosen)),
                **{f"mode_{m}": ";".join(alt[m]) if alt[m] is not None else "n/a" for m in MODES},
                "flags": " | ".join(flags) if flags else "OK",
            })
        for ses_label, pairs in pairs_by_ses.items():
            for p in sorted(pairs):
                if (ses_label, p) not in used_pairs:
                    unused_pairs += 1
                    issues.append(("WARN", ses_label, "", f"fieldmap pair not used by any run: {p}"))

    if n_nodate:
        issues.append(("WARN", "", "", f"{n_nodate} fieldmap sidecar(s) have no AcquisitionDate: times are time-of-day only, "
                                       "so ordering across sessions (ses-combined) relies on ShimSetting/OriginalSession"))

    n_fail = sum(1 for i in issues if i[0] == "FAIL")
    n_warn = sum(1 for i in issues if i[0] == "WARN")
    status = "FAIL" if n_fail else ("WARN" if n_warn else "OK")

    a.out_dir.mkdir(parents=True, exist_ok=True)
    tsv = a.out_dir / f"intendedfor_sub-{sub}_{layout}.tsv"
    cols = list(rows[0].keys()) if rows else ["layout"]
    with open(tsv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter="\t")
        w.writeheader()
        w.writerows(rows)

    md = a.out_dir / f"bids_setup_sub-{sub}_{layout}.md"
    with open(md, "w") as fh:
        fh.write(f"# BIDS setup QC: sub-{sub}, layout {layout}\n\n")
        fh.write(f"**Status: {status}**  ({n_fail} FAIL, {n_warn} WARN; {len(rows)} functional runs; IntendedFor mode used: `{a.mode}`; "
                 f"SyN-SDC {'enabled' if a.syn_sdc else 'NOT enabled'})\n\n")
        for lvl in ("FAIL", "WARN"):
            sel = [i for i in issues if i[0] == lvl]
            if sel:
                fh.write(f"## {lvl}\n\n")
                for _, s, r, msg in sel:
                    fh.write(f"- {(s + ' ' + r).strip()}: {msg}\n" if (s or r) else f"- {msg}\n")
                fh.write("\n")
        fh.write("## Runs\n\n| session | run | PE | fieldmaps in session | mapped pair(s) | " + " | ".join(MODES) + " | flags |\n")
        fh.write("|" + "---|" * (6 + len(MODES)) + "\n")
        for r in rows:
            fh.write(f"| {r['session']} | {r['run']} | {r['phase_encoding']} | {r['n_fmaps_in_session']} | {r['mapped_pairs']} | "
                     + " | ".join(r[f"mode_{m}"] for m in MODES) + f" | {r['flags']} |\n")

    summ = a.out_dir / "bids_setup_summary.tsv"
    head = ["subject", "layout", "mode", "n_runs", "n_fail", "n_warn", "n_unused_fmap_pairs", "status"]
    keep = []
    if summ.exists():
        with open(summ) as fh:
            keep = [r for r in csv.DictReader(fh, delimiter="\t") if not (r["subject"] == f"sub-{sub}" and r["layout"] == layout)]
    keep.append({"subject": f"sub-{sub}", "layout": layout, "mode": a.mode, "n_runs": len(rows), "n_fail": n_fail,
                 "n_warn": n_warn, "n_unused_fmap_pairs": unused_pairs, "status": status})
    with open(summ, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=head, delimiter="\t")
        w.writeheader()
        w.writerows(keep)

    print(f"[qc] {layout}: {status} ({n_fail} FAIL, {n_warn} WARN, {len(rows)} runs) -> {md}")
    for lvl, s, r, msg in issues:
        if lvl == "FAIL" or n_warn <= 8:
            print(f"[qc]   {lvl}: {(s + ' ' + r).strip()} {msg}".rstrip())
    sys.exit(2 if n_fail else 0)


if __name__ == "__main__":
    main()
