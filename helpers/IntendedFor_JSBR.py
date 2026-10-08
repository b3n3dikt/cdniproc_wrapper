#!/usr/bin/env python3
"""
IntendedFor_JSBR.py  (ShimSetting-gated, block-aware, AP/PA mirroring, no-subject paths)

Populate fmap/*.json "IntendedFor" by assigning each functional run (grouped across echoes)
to EPI fieldmaps in the same session. Primary gating uses ShimSetting equality to prevent
cross-session contamination (e.g., in ses-combined). Within each ShimSetting group, the
assignment is *block-aware*: each fieldmap is labeled as a pre- or post-block scan based on
local timeline density; runs inside its window are matched to it (with geometry/PE preference).
AP/PA pairs mirror IntendedFor lists.

IntendedFor entries are written WITHOUT the leading subject folder, e.g.:
  "ses-7T2/func/sub-XYZ_ses-7T2_task-rest_run-04_echo-1_bold.nii.gz"

Usage examples:
  # dry run
  python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1
  # write changes
  python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1 --write
  # write with backups
  python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1 --write --backup
"""

import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

FLOAT_TOL = 1e-6

# ---------------- I/O helpers ----------------

def read_json(p: Path):
    try:
        with p.open("r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to read {p}: {e}", file=sys.stderr)
        return {}

def write_json(p: Path, obj, backup=False):
    try:
        if backup and p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_text(p.read_text())
        with p.open("w") as f:
            json.dump(obj, f, indent=4)
            f.write("\n")
    except Exception as e:
        print(f"[ERROR] Failed to write {p}: {e}", file=sys.stderr)

# ---------------- time / name helpers ----------------

def seconds_of_day(acq_time: str):
    if not acq_time:
        return None
    try:
        if "." in acq_time:
            base, frac = acq_time.split(".")
            frac = (frac + "000000")[:6]
            acq_time = f"{base}.{frac}"
            t = datetime.strptime(acq_time, "%H:%M:%S.%f")
        else:
            t = datetime.strptime(acq_time, "%H:%M:%S")
        return t.hour * 3600 + t.minute * 60 + t.second + (t.microsecond / 1e6)
    except Exception:
        return None

def parse_date(s):
    if not s:
        return None
    try:
        if "-" in s:
            return datetime.strptime(s, "%Y-%m-%d").date()
        return datetime.strptime(s, "%Y%m%d").date()
    except Exception:
        return None

def parse_time_any(s):
    if not s:
        return None
    s = s.strip()
    try:
        if ":" in s:
            if "." in s:
                base, frac = s.split(".")
                frac = (frac + "000000")[:6]
                return datetime.strptime(f"{base}.{frac}", "%H:%M:%S.%f").time()
            return datetime.strptime(s, "%H:%M:%S").time()
        else:
            if "." in s:
                base, frac = s.split(".")
                frac = (frac + "000000")[:6]
                return datetime.strptime(f"{base}.{frac}", "%H%M%S.%f").time()
            return datetime.strptime(s, "%H%M%S").time()
    except Exception:
        return None

def parse_datetime_any(s):
    if not s:
        return None
    s = s.strip().replace("T", " ")
    try:
        if "." in s:
            base, frac = s.split(".")
            frac = (frac + "000000")[:6]
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y%m%d %H%M%S.%f"):
                try:
                    return datetime.strptime(f"{base}.{frac}", fmt)
                except Exception:
                    pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H%M%S"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
    except Exception:
        pass
    return None

def get_timestamp(meta: dict, path: Path):
    """
    Seconds since epoch from (in order): AcquisitionDateTime;
    AcquisitionDate + AcquisitionTime; AcquisitionTime only (epoch day);
    SeriesNumber proxy; file mtime fallback.
    """
    s = meta.get("AcquisitionDateTime")
    dt = parse_datetime_any(s) if s else None
    if dt:
        return dt.timestamp()

    d = parse_date(meta.get("AcquisitionDate"))
    t = parse_time_any(meta.get("AcquisitionTime"))
    if d and t:
        try:
            return datetime.combine(d, t).timestamp()
        except Exception:
            pass

    if t:
        try:
            return datetime.combine(date(1970,1,1), t).timestamp()
        except Exception:
            pass

    try:
        s_num = float(meta.get("SeriesNumber"))
        return 1_000_000.0 + s_num * 60.0
    except Exception:
        pass

    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0

def strip_echo_and_part(basename: str) -> str:
    no_echo = re.sub(r"_echo-\d+\b", "", basename)
    return re.sub(r"_part-(mag|phase)\b", "", no_echo)

def opposite_pe(pe: str):
    if not pe:
        return None
    return pe[:-1] if pe.endswith("-") else pe + "-"

def approx_equal(a, b, tol=FLOAT_TOL):
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return a == b

def listify(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]

# ---------------- geometry + shim ----------------

def geom_signature(meta: dict):
    return {
        "BaseResolution": meta.get("BaseResolution"),
        "AcquisitionMatrixPE": meta.get("AcquisitionMatrixPE"),
        "ReconMatrixPE": meta.get("ReconMatrixPE"),
        "TotalReadoutTime": meta.get("TotalReadoutTime"),
        "EffectiveEchoSpacing": meta.get("EffectiveEchoSpacing"),
        "ParallelReductionFactorInPlane": meta.get("ParallelReductionFactorInPlane"),
        "MultibandAccelerationFactor": meta.get("MultibandAccelerationFactor"),
        "SliceThickness": meta.get("SliceThickness"),
        "SpacingBetweenSlices": meta.get("SpacingBetweenSlices"),
        "ImageOrientationPatientDICOM": meta.get("ImageOrientationPatientDICOM"),
        "PhaseEncodingDirection": meta.get("PhaseEncodingDirection"),
    }

def geom_compatible(fmap_meta: dict, func_meta: dict) -> bool:
    A = geom_signature(fmap_meta)
    B = geom_signature(func_meta)

    if A.get("PhaseEncodingDirection") and B.get("PhaseEncodingDirection"):
        if A["PhaseEncodingDirection"] != opposite_pe(B["PhaseEncodingDirection"]):
            return False

    scalar_keys = [
        "BaseResolution",
        "AcquisitionMatrixPE",
        "ReconMatrixPE",
        "TotalReadoutTime",
        "EffectiveEchoSpacing",
        "ParallelReductionFactorInPlane",
        "MultibandAccelerationFactor",
        "SliceThickness",
        "SpacingBetweenSlices",
    ]
    for k in scalar_keys:
        av = A.get(k); bv = B.get(k)
        if av is None or bv is None:
            continue
        tol = 1e-5 if ("Time" in k or "Spacing" in k) else 1e-3
        if not approx_equal(av, bv, tol=tol):
            return False

    a_ori = listify(A.get("ImageOrientationPatientDICOM"))
    b_ori = listify(B.get("ImageOrientationPatientDICOM"))
    if a_ori and b_ori:
        if len(a_ori) != len(b_ori):
            return False
        for av, bv in zip(a_ori, b_ori):
            if not approx_equal(av, bv, tol=1e-4):
                return False

    return True

def shim_key(meta: dict):
    """Return ShimSetting as an immutable tuple (or None if missing/invalid)."""
    ss = meta.get("ShimSetting")
    if isinstance(ss, (list, tuple)) and len(ss) > 0:
        try:
            return tuple(int(round(float(x))) for x in ss)
        except Exception:
            # keep exact values if they are already ints
            try:
                return tuple(ss)
            except Exception:
                return None
    return None

# ---------------- discovery ----------------

def _normalize_label_items(items, prefix):
    if not items:
        return None
    out = set()
    for it in items:
        name = it
        try:
            p = Path(it)
            parts = [part for part in p.parts if part.startswith(prefix)]
            if parts:
                name = parts[-1]
            else:
                name = p.name
        except Exception:
            pass
        if not name.startswith(prefix):
            name = f"{prefix}{name}"
        out.add(name)
    return out or None

def find_files(bids_root: Path, sub_filter=None, ses_filter=None):
    """
    Return dict keyed by (sub, ses) with lists of fmap jsons and func jsons.
    ses may be None for subject-level datasets without sessions.
    """
    result = defaultdict(lambda: {"fmap_jsons": [], "func_jsons": []})

    subs = [p for p in bids_root.glob("sub-*") if p.is_dir()]
    if sub_filter:
        subs = [p for p in subs if p.name in sub_filter]

    for sub_path in subs:
        ses_dirs = list(sub_path.glob("ses-*"))
        ses_paths = ses_dirs if ses_dirs else [sub_path]

        if ses_filter and ses_dirs:
            ses_paths = [p for p in ses_paths if p.name in ses_filter]

        for ses_path in ses_paths:
            key = (sub_path.name, ses_path.name if ses_dirs else None)
            for j in ses_path.glob("fmap/*_epi.json"):
                result[key]["fmap_jsons"].append(j)
            for j in ses_path.glob("func/*_bold.json"):
                result[key]["func_jsons"].append(j)

    return result

# ---------------- assignment primitives ----------------

def within_gap(run_t, fm_t, max_gap):
    if max_gap <= 0:
        return True
    return abs(run_t - fm_t) <= max_gap

def median_abs_deltas(times, ref):
    if not times:
        return float("inf")
    deltas = sorted(abs(t - ref) for t in times)
    n = len(deltas)
    mid = n // 2
    if n % 2 == 1:
        return deltas[mid]
    return 0.5 * (deltas[mid - 1] + deltas[mid])

def drop_subject_prefix(path_str: str) -> str:
    if path_str.startswith("sub-") and "/" in path_str:
        return path_str.split("/", 1)[1]
    return path_str

def intended_rel_without_sub(nii_path: Path, bids_root: Path) -> str:
    try:
        rel = nii_path.relative_to(bids_root).as_posix()
    except Exception:
        rel = nii_path.as_posix()
    return drop_subject_prefix(rel)

DIR_RE = re.compile(r"_dir-[^_]+")

def pair_key_from_name(fname: str) -> str:
    base = fname[:-5] if fname.endswith(".json") else fname
    return DIR_RE.sub("", base)

def is_mag_json(path: Path) -> bool:
    if "_part-" not in path.name:
        return True
    return "_part-mag" in path.name

# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bids_root", type=Path, help="Path to BIDS root (directory containing sub-*/)")
    ap.add_argument("--write", action="store_true", help="Write IntendedFor into fmap jsons (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="Print mapping only (default if --write not given)")
    ap.add_argument("--sub", dest="subs", action="append", help="Limit to specific subject(s): 'sub-01' or path to sub-01")
    ap.add_argument("--ses", dest="sess", action="append", help="Limit to specific session(s): 'ses-7T1' or path to ses-7T1")
    ap.add_argument("--backup", action="store_true", help="Also write .bak alongside modified JSONs (default: off)")
    ap.add_argument("--assign-mode", choices=["auto", "nearest", "lookback", "lookforward"], default="auto",
                    help="auto=block-aware; nearest=closest fmap; lookback=use previous fmap; lookforward=use next fmap.")
    ap.add_argument("--max-gap-sec", type=float, default=1800.0,
                    help="Ignore funcs farther than this many seconds from an fmap when deciding pre/post (0 disables).")
    ap.add_argument("--ignore-shim", action="store_true",
                    help="Do not gate by ShimSetting; allow cross-shim matching (not recommended for ses-combined).")
    args = ap.parse_args()

    if not args.write:
        args.dry_run = True

    root = args.bids_root
    if not root.exists():
        print(f"[ERROR] {root} does not exist", file=sys.stderr)
        sys.exit(1)

    sub_filter = _normalize_label_items(args.subs, "sub-")
    ses_filter = _normalize_label_items(args.sess, "ses-")

    inventory = find_files(root, sub_filter=sub_filter, ses_filter=ses_filter)

    total_links = 0
    for (sub, ses), lists in sorted(inventory.items()):
        if not lists["fmap_jsons"]:
            continue

        hdr = f"{sub} {ses or ''}".strip()
        print(f"\n=== {hdr} ===")

        # Load fmaps with metadata
        fmap_entries = []
        for jpath in sorted(lists["fmap_jsons"]):
            meta = read_json(jpath)
            nifti = jpath.with_suffix("").with_suffix(".nii.gz")
            fmap_entries.append({
                "json": jpath,
                "nii": nifti,
                "meta": meta,
                "time": get_timestamp(meta, jpath),
                "shim": shim_key(meta),
            })

        # Load func entries, prefer magnitude, group by run (echo-insensitive)
        raw_func_entries = []
        for jpath in sorted(lists["func_jsons"]):
            if not is_mag_json(jpath):
                continue
            meta = read_json(jpath)
            nifti = jpath.with_suffix("").with_suffix(".nii.gz")
            raw_func_entries.append({
                "json": jpath,
                "nii": nifti,
                "meta": meta,
                "time": get_timestamp(meta, jpath),
                "shim": shim_key(meta),
            })

        func_groups = defaultdict(list)
        for fe in raw_func_entries:
            key = strip_echo_and_part(fe["json"].name)
            func_groups[key].append(fe)

        # Representative per run = earliest by timestamp, tiebreak by EchoNumber
        run_reps = []
        for gkey, files in func_groups.items():
            rep = min(files, key=lambda x: (x["time"], x["meta"].get("EchoNumber", 0)))
            run_reps.append({"gkey": gkey, "rep": rep})
        if not run_reps:
            print("No usable functional runs found (magnitude BOLD JSONs).")
            continue

        # Sort fmaps and runs by time
        fmap_entries.sort(key=lambda x: x["time"])
        run_reps.sort(key=lambda x: x["rep"]["time"])

        # Build AP/PA pair index (for mirroring later)
        pair_members = defaultdict(list)
        for fm in fmap_entries:
            pk = pair_key_from_name(fm["json"].name)
            pair_members[pk].append(fm["json"])

        # Group by shim
        if args.ignore_shim:
            shim_keys = {None}
            fmaps_by_shim = {None: fmap_entries}
            runs_by_shim = {None: run_reps}
        else:
            fmaps_by_shim = defaultdict(list)
            for fm in fmap_entries:
                fmaps_by_shim[fm["shim"]].append(fm)
            runs_by_shim = defaultdict(list)
            for rr in run_reps:
                runs_by_shim[rr["rep"]["shim"]].append(rr)
            shim_keys = set(runs_by_shim.keys())  # only process shims that have runs

        # Container: fmap -> set of IntendedFor targets
        fmap_to_intended = defaultdict(set)

        # Helper to add all echoes of a run to a chosen fmap
        def add_run_to_fmap(fmap_obj, group_key):
            for fe in sorted(func_groups[group_key], key=lambda x: x["meta"].get("EchoNumber", 0)):
                rel_no_sub = intended_rel_without_sub(fe["nii"], root)
                fmap_to_intended[fmap_obj["json"]].add(rel_no_sub)

        # Assignment inside each shim group
        for sk in sorted(shim_keys, key=lambda x: (str(type(x)), x)):
            group_fmaps = fmaps_by_shim.get(sk, [])
            group_runs  = runs_by_shim.get(sk, [])

            if not group_runs:
                continue

            # Fallback: if no fmaps for this shim, warn and allow global pool
            pool_fmaps = group_fmaps if group_fmaps else fmap_entries
            if not group_fmaps:
                print(f"[WARN] No fmap with matching ShimSetting for shim={sk}; "
                      f"falling back to global pool for {len(group_runs)} run(s).")

            # Sort (again) by time inside group
            pool_fmaps = sorted(pool_fmaps, key=lambda x: x["time"])
            group_runs = sorted(group_runs, key=lambda x: x["rep"]["time"])

            # Build timeline arrays
            fm_times = [fm["time"] for fm in pool_fmaps]
            run_times = [r["rep"]["time"] for r in group_runs]

            # Decide per-fmap pre/post label
            fm_labels = []
            for j, fm in enumerate(pool_fmaps):
                if args.assign_mode == "lookback":
                    fm_labels.append("post")
                    continue
                if args.assign_mode == "lookforward":
                    fm_labels.append("pre")
                    continue
                if args.assign_mode == "nearest":
                    fm_labels.append("pre")  # ignored in nearest mode
                    continue

                left_start = fm_times[j - 1] if j > 0 else float("-inf")
                left_end   = fm_times[j]
                right_start = fm_times[j]
                right_end   = fm_times[j + 1] if j + 1 < len(fm_times) else float("inf")

                left_runs  = [t for t in run_times if (t > left_start and t <= left_end)]
                right_runs = [t for t in run_times if (t > right_start and t <= right_end)]

                lc, rc = len(left_runs), len(right_runs)
                if rc > lc:
                    fm_labels.append("pre")
                elif lc > rc:
                    fm_labels.append("post")
                else:
                    mleft  = median_abs_deltas(left_runs,  fm["time"])
                    mright = median_abs_deltas(right_runs, fm["time"])
                    fm_labels.append("pre" if mright <= mleft else "post")

            # Assign runs in this shim group
            for r in group_runs:
                rep = r["rep"]
                rt  = rep["time"]

                # Candidate set based on mode
                candidates = []
                if args.assign_mode == "nearest":
                    candidates = pool_fmaps[:]
                else:
                    # window of the "owning" fmap (pre/post)
                    for j, fm in enumerate(pool_fmaps):
                        lbl = fm_labels[j]
                        if lbl == "post":
                            start = fm_times[j - 1] if j > 0 else float("-inf")
                            end   = fm_times[j]
                            in_window = (rt > start and rt <= end)
                        else:
                            start = fm_times[j]
                            end   = fm_times[j + 1] if j + 1 < len(fm_times) else float("inf")
                            in_window = (rt > start and rt <= end)
                        if in_window:
                            candidates.append(fm)
                    if not candidates:
                        candidates = pool_fmaps[:]

                # Prioritize opposite PE + geometry
                pe = rep["meta"].get("PhaseEncodingDirection")
                want_pe = opposite_pe(pe) if pe else None
                cands = []
                if want_pe:
                    for fm in candidates:
                        if fm["meta"].get("PhaseEncodingDirection") == want_pe and geom_compatible(fm["meta"], rep["meta"]):
                            cands.append(fm)
                if not cands and want_pe:
                    cands = [fm for fm in candidates if fm["meta"].get("PhaseEncodingDirection") == want_pe]
                if not cands:
                    cands = candidates

                chosen = min(cands, key=lambda fm: abs(rt - fm["time"]))
                add_run_to_fmap(chosen, r["gkey"])

        # ---- MIRROR: union IntendedFor across AP/PA pairs ----
        pair_to_union = defaultdict(set)
        for fmap_json, targets in fmap_to_intended.items():
            pk = pair_key_from_name(fmap_json.name)
            pair_to_union[pk].update(targets)

        final_targets_per_json = {}
        for pk, union_targets in pair_to_union.items():
            for member_json in pair_members.get(pk, []):
                final_targets_per_json[member_json] = sorted(union_targets)

        # Emit + write
        for jpath in sorted(final_targets_per_json.keys(), key=lambda p: p.name):
            meta = read_json(jpath)
            new_list = final_targets_per_json[jpath]
            meta["IntendedFor"] = new_list
            total_links += len(new_list)

            try:
                relp = jpath.relative_to(root)
            except Exception:
                relp = jpath
            print(f"\n[fmap] {relp}")
            for t in new_list:
                print(f"  - {t}")

            if args.write:
                write_json(jpath, meta, backup=args.backup)

    print(f"\nDone. {'Wrote' if not args.dry_run else 'Planned'} {total_links} IntendedFor links.")

if __name__ == "__main__":
    main()

# #!/usr/bin/env python3
# """
# IntendedFor_JSBR.py

# Populate fmap/*.json "IntendedFor" by assigning each functional run (grouped across echoes)
# to the closest-in-time, opposite-PE EPI fieldmap in the same session, requiring compatible
# geometry/readout. Also mirrors IntendedFor lists across AP/PA pairs if both exist.

# IntendedFor entries are written WITHOUT the leading subject folder, e.g.:
#   "ses-7T2/func/sub-XYZ_ses-7T2_task-rest_run-04_echo-1_bold.nii.gz"

# Usage examples:
#   # dry run
#   python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1
#   # write changes, no backups
#   python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1 --write
#   # write changes, with backups
#   python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1 --write --backup
# """

# import argparse
# import json
# import re
# import sys
# from pathlib import Path
# from datetime import datetime
# from collections import defaultdict

# FLOAT_TOL = 1e-6

# # ---------------- I/O helpers ----------------

# def read_json(p: Path):
#     try:
#         with p.open("r") as f:
#             return json.load(f)
#     except Exception as e:
#         print(f"[WARN] Failed to read {p}: {e}", file=sys.stderr)
#         return {}

# def write_json(p: Path, obj, backup=False):
#     try:
#         if backup and p.exists():
#             bak = p.with_suffix(p.suffix + ".bak")
#             bak.write_text(p.read_text())
#         with p.open("w") as f:
#             json.dump(obj, f, indent=4)
#             f.write("\n")
#     except Exception as e:
#         print(f"[ERROR] Failed to write {p}: {e}", file=sys.stderr)

# # ---------------- time / name helpers ----------------

# def seconds_of_day(acq_time: str):
#     if not acq_time:
#         return None
#     try:
#         if "." in acq_time:
#             base, frac = acq_time.split(".")
#             frac = (frac + "000000")[:6]
#             acq_time = f"{base}.{frac}"
#             t = datetime.strptime(acq_time, "%H:%M:%S.%f")
#         else:
#             t = datetime.strptime(acq_time, "%H:%M:%S")
#         return t.hour * 3600 + t.minute * 60 + t.second + (t.microsecond / 1e6)
#     except Exception:
#         return None

# def strip_echo(basename: str) -> str:
#     return re.sub(r"_echo-\d+\b", "", basename)

# def opposite_pe(pe: str):
#     if not pe:
#         return None
#     return pe[:-1] if pe.endswith("-") else pe + "-"

# def approx_equal(a, b, tol=FLOAT_TOL):
#     try:
#         return abs(float(a) - float(b)) <= tol
#     except Exception:
#         return a == b

# def listify(x):
#     if x is None:
#         return []
#     return x if isinstance(x, list) else [x]

# # ---------------- geometry matching ----------------

# def geom_signature(meta: dict):
#     return {
#         "BaseResolution": meta.get("BaseResolution"),
#         "AcquisitionMatrixPE": meta.get("AcquisitionMatrixPE"),
#         "ReconMatrixPE": meta.get("ReconMatrixPE"),
#         "TotalReadoutTime": meta.get("TotalReadoutTime"),
#         "EffectiveEchoSpacing": meta.get("EffectiveEchoSpacing"),
#         "ParallelReductionFactorInPlane": meta.get("ParallelReductionFactorInPlane"),
#         "MultibandAccelerationFactor": meta.get("MultibandAccelerationFactor"),
#         "SliceThickness": meta.get("SliceThickness"),
#         "SpacingBetweenSlices": meta.get("SpacingBetweenSlices"),
#         "ImageOrientationPatientDICOM": meta.get("ImageOrientationPatientDICOM"),
#         "PhaseEncodingDirection": meta.get("PhaseEncodingDirection"),
#     }

# def geom_compatible(fmap_meta: dict, func_meta: dict) -> bool:
#     A = geom_signature(fmap_meta)
#     B = geom_signature(func_meta)

#     if A.get("PhaseEncodingDirection") and B.get("PhaseEncodingDirection"):
#         if A["PhaseEncodingDirection"] != opposite_pe(B["PhaseEncodingDirection"]):
#             return False

#     scalar_keys = [
#         "BaseResolution",
#         "AcquisitionMatrixPE",
#         "ReconMatrixPE",
#         "TotalReadoutTime",
#         "EffectiveEchoSpacing",
#         "ParallelReductionFactorInPlane",
#         "MultibandAccelerationFactor",
#         "SliceThickness",
#         "SpacingBetweenSlices",
#     ]
#     for k in scalar_keys:
#         av = A.get(k); bv = B.get(k)
#         if av is None or bv is None:
#             continue
#         tol = 1e-5 if "Time" in k or "Spacing" in k else 1e-3
#         if not approx_equal(av, bv, tol=tol):
#             return False

#     a_ori = listify(A.get("ImageOrientationPatientDICOM"))
#     b_ori = listify(B.get("ImageOrientationPatientDICOM"))
#     if a_ori and b_ori:
#         if len(a_ori) != len(b_ori):
#             return False
#         for av, bv in zip(a_ori, b_ori):
#             if not approx_equal(av, bv, tol=1e-4):
#                 return False

#     return True

# # ---------------- discovery ----------------

# def _normalize_label_items(items, prefix):
#     """
#     Accept entries like 'ses-7T1', '7T1', '/path/.../ses-7T1', and return a set of canonical names.
#     For subs, prefix='sub-'; for sessions, prefix='ses-'.
#     """
#     if not items:
#         return None
#     out = set()
#     for it in items:
#         name = it
#         try:
#             p = Path(it)
#             parts = [part for part in p.parts if part.startswith(prefix)]
#             if parts:
#                 name = parts[-1]
#             else:
#                 name = p.name
#         except Exception:
#             pass
#         if not name.startswith(prefix):
#             name = f"{prefix}{name}"
#         out.add(name)
#     return out or None

# def find_files(bids_root: Path, sub_filter=None, ses_filter=None):
#     """
#     Return dict keyed by (sub, ses) with lists of fmap jsons and func jsons.
#     - sub_filter/ses_filter are sets like {'sub-01', ...} / {'ses-7T1', ...}
#     """
#     result = defaultdict(lambda: {"fmap_jsons": [], "func_jsons": []})

#     subs = [p for p in bids_root.glob("sub-*") if p.is_dir()]
#     if sub_filter:
#         subs = [p for p in subs if p.name in sub_filter]

#     for sub_path in subs:
#         ses_dirs = list(sub_path.glob("ses-*"))
#         ses_paths = ses_dirs if ses_dirs else [sub_path]

#         if ses_filter and ses_dirs:
#             ses_paths = [p for p in ses_paths if p.name in ses_filter]

#         for ses_path in ses_paths:
#             key = (sub_path.name, ses_path.name if ses_dirs else None)
#             for j in ses_path.glob("fmap/*_epi.json"):
#                 result[key]["fmap_jsons"].append(j)
#             for j in ses_path.glob("func/*_bold.json"):
#                 result[key]["func_jsons"].append(j)

#     return result

# # ---------------- assignment ----------------

# def choose_nearest_fmap(func_meta, fmap_candidates):
#     func_time = seconds_of_day(func_meta.get("AcquisitionTime"))
#     func_series = func_meta.get("SeriesNumber")
#     best = None
#     best_key = (float("inf"), float("inf"))
#     for fm in fmap_candidates:
#         fm_time = seconds_of_day(fm["meta"].get("AcquisitionTime"))
#         fm_series = fm["meta"].get("SeriesNumber")
#         dt = abs(func_time - fm_time) if (func_time is not None and fm_time is not None) else float("inf")
#         ds = abs(int(func_series) - int(fm_series)) if (func_series is not None and fm_series is not None) else float("inf")
#         key = (dt, ds)
#         if key < best_key:
#             best_key = key
#             best = fm
#     return best

# # ---- IntendedFor path helpers ----

# def drop_subject_prefix(path_str: str) -> str:
#     """Remove leading 'sub-.../' from a POSIX path string if present."""
#     if path_str.startswith("sub-") and "/" in path_str:
#         return path_str.split("/", 1)[1]
#     return path_str

# def intended_rel_without_sub(nii_path: Path, bids_root: Path) -> str:
#     """Return subject-relative path like 'ses-XXX/func/...nii.gz' (no leading subject)."""
#     try:
#         rel = nii_path.relative_to(bids_root).as_posix()
#     except Exception:
#         rel = nii_path.as_posix()
#     return drop_subject_prefix(rel)

# # ---- Pairing helpers (AP/PA mirroring) ----

# DIR_RE = re.compile(r"_dir-[^_]+")

# def pair_key_from_name(fname: str) -> str:
#     """
#     Build a pairing key for fmap filenames by removing the '_dir-XXX' segment.
#     Works on the basename string (with or without extension).
#     """
#     base = fname
#     if base.endswith(".json"):
#         base = base[:-5]
#     return DIR_RE.sub("", base)

# # ---------------- main ----------------

# def main():
#     ap = argparse.ArgumentParser()
#     ap.add_argument("bids_root", type=Path, help="Path to BIDS root (directory containing sub-*/)")
#     ap.add_argument("--write", action="store_true", help="Write IntendedFor into fmap jsons (default: dry-run)")
#     ap.add_argument("--dry-run", action="store_true", help="Print mapping only (default if --write not given)")
#     ap.add_argument("--sub", dest="subs", action="append", help="Limit to specific subject(s): 'sub-01' or path to sub-01")
#     ap.add_argument("--ses", dest="sess", action="append", help="Limit to specific session(s): 'ses-7T1' or path to ses-7T1")
#     ap.add_argument("--backup", action="store_true", help="Also write .bak alongside modified JSONs (default: off)")
#     args = ap.parse_args()

#     if not args.write:
#         args.dry_run = True

#     root = args.bids_root
#     if not root.exists():
#         print(f"[ERROR] {root} does not exist", file=sys.stderr)
#         sys.exit(1)

#     sub_filter = _normalize_label_items(args.subs, "sub-")
#     ses_filter = _normalize_label_items(args.sess, "ses-")

#     inventory = find_files(root, sub_filter=sub_filter, ses_filter=ses_filter)

#     total_links = 0
#     for (sub, ses), lists in sorted(inventory.items()):
#         if not lists["fmap_jsons"]:
#             continue

#         hdr = f"{sub} {ses or ''}".strip()
#         print(f"\n=== {hdr} ===")

#         # Load fmap + func entries
#         fmap_entries = []
#         for jpath in sorted(lists["fmap_jsons"]):
#             meta = read_json(jpath)
#             nifti = jpath.with_suffix("").with_suffix(".nii.gz")
#             fmap_entries.append({"json": jpath, "nii": nifti, "meta": meta})

#         func_entries = []
#         for jpath in sorted(lists["func_jsons"]):
#             meta = read_json(jpath)
#             nifti = jpath.with_suffix("").with_suffix(".nii.gz")
#             func_entries.append({"json": jpath, "nii": nifti, "meta": meta})

#         # Group func echoes by run key (basename without echo)
#         func_groups = defaultdict(list)
#         for fe in func_entries:
#             key = strip_echo(fe["json"].name)
#             func_groups[key].append(fe)

#         # Index fmaps by PE and also build AP/PA "pairs"
#         fmap_by_dir = defaultdict(list)
#         pair_members = defaultdict(list)  # pair_key -> list[Path to json]
#         for fm in fmap_entries:
#             pe = fm["meta"].get("PhaseEncodingDirection")
#             if pe:
#                 fmap_by_dir[pe].append(fm)
#             pk = pair_key_from_name(fm["json"].name)
#             pair_members[pk].append(fm["json"])

#         # Assign runs -> chosen fmap (as before)
#         fmap_to_intended = defaultdict(set)

#         for gkey, group_files in sorted(func_groups.items()):
#             # representative echo (first echo)
#             rep = sorted(group_files, key=lambda x: x["meta"].get("EchoNumber", 0))[0]
#             func_pe = rep["meta"].get("PhaseEncodingDirection")
#             want_pe = opposite_pe(func_pe) if func_pe else None

#             # geometry-compatible candidates, then relax if needed
#             candidates = []
#             if want_pe and want_pe in fmap_by_dir:
#                 for fm in fmap_by_dir[want_pe]:
#                     if geom_compatible(fm["meta"], rep["meta"]):
#                         candidates.append(fm)
#             if not candidates and want_pe and want_pe in fmap_by_dir:
#                 candidates = fmap_by_dir[want_pe][:]
#             if not candidates:
#                 candidates = fmap_entries[:]

#             chosen = choose_nearest_fmap(rep["meta"], candidates)
#             if not chosen:
#                 print(f"[WARN] No fmap choice for run {gkey} (func {rep['json'].name})")
#                 continue

#             # add all echoes of this run
#             for fe in sorted(group_files, key=lambda x: x["meta"].get("EchoNumber", 0)):
#                 rel_no_sub = intended_rel_without_sub(fe["nii"], root)
#                 fmap_to_intended[chosen["json"]].add(rel_no_sub)

#             ft = seconds_of_day(rep["meta"].get("AcquisitionTime"))
#             ct = seconds_of_day(chosen["meta"].get("AcquisitionTime"))
#             dt_str = f" (dt={abs(ft-ct):.1f}s)" if (ft is not None and ct is not None) else ""
#             print(f"Map {gkey} -> {chosen['json'].name}{dt_str}")

#         # ---- MIRROR: union IntendedFor across AP/PA pairs ----
#         pair_to_union = defaultdict(set)
#         for fmap_json, targets in fmap_to_intended.items():
#             pk = pair_key_from_name(fmap_json.name)
#             pair_to_union[pk].update(targets)

#         # Prepare final write set per fmap json
#         final_targets_per_json = {}
#         for pk, union_targets in pair_to_union.items():
#             for member_json in pair_members.get(pk, []):
#                 final_targets_per_json[member_json] = sorted(union_targets)

#         # Show + write
#         for jpath in sorted(final_targets_per_json.keys(), key=lambda p: p.name):
#             meta = read_json(jpath)
#             new_list = final_targets_per_json[jpath]
#             meta["IntendedFor"] = new_list
#             total_links += len(new_list)

#             print(f"\n[fmap] {jpath.relative_to(root)}")
#             for t in new_list:
#                 print(f"  - {t}")

#             if args.write:
#                 write_json(jpath, meta, backup=args.backup)

#     print(f"\nDone. {'Wrote' if not args.dry_run else 'Planned'} {total_links} IntendedFor links.")

# if __name__ == "__main__":
#     main()

# #!/usr/bin/env python3
# """
# IntendedFor_JSBR.py

# Populate fmap/*.json "IntendedFor" by assigning each functional run (grouped across echoes)
# to the closest-in-time, opposite-PE EPI fieldmap in the same session, requiring compatible
# geometry/readout. Robust to being given --sub/--ses as labels (sub-01, ses-02) or as full paths.

# IntendedFor entries are written WITHOUT the leading subject folder, e.g.:
#   "ses-7T2/func/sub-SUB001_ses-7T2_task-restNORDIC_run-04_echo-1_bold.nii.gz"

# Usage examples:
#   # dry run
#   python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1
#   # write changes
#   python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses /path/to/bidsroot/sub-01/ses-7T1 --write
# """

# import argparse
# import json
# import re
# import sys
# from pathlib import Path
# from datetime import datetime
# from collections import defaultdict

# FLOAT_TOL = 1e-6

# # ---------------- I/O helpers ----------------

# def read_json(p: Path):
#     try:
#         with p.open("r") as f:
#             return json.load(f)
#     except Exception as e:
#         print(f"[WARN] Failed to read {p}: {e}", file=sys.stderr)
#         return {}

# def write_json(p: Path, obj, backup=True):
#     try:
#         if backup and p.exists():
#             bak = p.with_suffix(p.suffix + ".bak")
#             bak.write_text(p.read_text())
#         with p.open("w") as f:
#             json.dump(obj, f, indent=4)
#             f.write("\n")
#     except Exception as e:
#         print(f"[ERROR] Failed to write {p}: {e}", file=sys.stderr)

# # ---------------- time / name helpers ----------------

# def seconds_of_day(acq_time: str):
#     if not acq_time:
#         return None
#     try:
#         if "." in acq_time:
#             base, frac = acq_time.split(".")
#             frac = (frac + "000000")[:6]
#             acq_time = f"{base}.{frac}"
#             t = datetime.strptime(acq_time, "%H:%M:%S.%f")
#         else:
#             t = datetime.strptime(acq_time, "%H:%M:%S")
#         return t.hour * 3600 + t.minute * 60 + t.second + (t.microsecond / 1e6)
#     except Exception:
#         return None

# def strip_echo(basename: str) -> str:
#     return re.sub(r"_echo-\d+\b", "", basename)

# def opposite_pe(pe: str):
#     if not pe:
#         return None
#     return pe[:-1] if pe.endswith("-") else pe + "-"

# def approx_equal(a, b, tol=FLOAT_TOL):
#     try:
#         return abs(float(a) - float(b)) <= tol
#     except Exception:
#         return a == b

# def listify(x):
#     if x is None:
#         return []
#     return x if isinstance(x, list) else [x]

# # ---------------- geometry matching ----------------

# def geom_signature(meta: dict):
#     return {
#         "BaseResolution": meta.get("BaseResolution"),
#         "AcquisitionMatrixPE": meta.get("AcquisitionMatrixPE"),
#         "ReconMatrixPE": meta.get("ReconMatrixPE"),
#         "TotalReadoutTime": meta.get("TotalReadoutTime"),
#         "EffectiveEchoSpacing": meta.get("EffectiveEchoSpacing"),
#         "ParallelReductionFactorInPlane": meta.get("ParallelReductionFactorInPlane"),
#         "MultibandAccelerationFactor": meta.get("MultibandAccelerationFactor"),
#         "SliceThickness": meta.get("SliceThickness"),
#         "SpacingBetweenSlices": meta.get("SpacingBetweenSlices"),
#         "ImageOrientationPatientDICOM": meta.get("ImageOrientationPatientDICOM"),
#         "PhaseEncodingDirection": meta.get("PhaseEncodingDirection"),
#     }

# def geom_compatible(fmap_meta: dict, func_meta: dict) -> bool:
#     A = geom_signature(fmap_meta)
#     B = geom_signature(func_meta)

#     if A.get("PhaseEncodingDirection") and B.get("PhaseEncodingDirection"):
#         if A["PhaseEncodingDirection"] != opposite_pe(B["PhaseEncodingDirection"]):
#             return False

#     scalar_keys = [
#         "BaseResolution",
#         "AcquisitionMatrixPE",
#         "ReconMatrixPE",
#         "TotalReadoutTime",
#         "EffectiveEchoSpacing",
#         "ParallelReductionFactorInPlane",
#         "MultibandAccelerationFactor",
#         "SliceThickness",
#         "SpacingBetweenSlices",
#     ]
#     for k in scalar_keys:
#         av = A.get(k); bv = B.get(k)
#         if av is None or bv is None:
#             continue
#         tol = 1e-5 if "Time" in k or "Spacing" in k else 1e-3
#         if not approx_equal(av, bv, tol=tol):
#             return False

#     a_ori = listify(A.get("ImageOrientationPatientDICOM"))
#     b_ori = listify(B.get("ImageOrientationPatientDICOM"))
#     if a_ori and b_ori:
#         if len(a_ori) != len(b_ori):
#             return False
#         for av, bv in zip(a_ori, b_ori):
#             if not approx_equal(av, bv, tol=1e-4):
#                 return False

#     return True

# # ---------------- discovery ----------------

# def _normalize_label_items(items, prefix):
#     """
#     Accept entries like 'ses-7T1', '7T1', '/path/.../ses-7T1', and return a set of canonical names.
#     For subs, prefix='sub-'; for sessions, prefix='ses-'.
#     """
#     if not items:
#         return None
#     out = set()
#     for it in items:
#         name = it
#         try:
#             p = Path(it)
#             parts = [part for part in p.parts if part.startswith(prefix)]
#             if parts:
#                 name = parts[-1]
#             else:
#                 name = p.name
#         except Exception:
#             pass
#         if not name.startswith(prefix):
#             name = f"{prefix}{name}"
#         out.add(name)
#     return out or None

# def find_files(bids_root: Path, sub_filter=None, ses_filter=None):
#     """
#     Return dict keyed by (sub, ses) with lists of fmap jsons and func jsons.
#     - sub_filter/ses_filter are sets like {'sub-01', ...} / {'ses-7T1', ...}
#     """
#     result = defaultdict(lambda: {"fmap_jsons": [], "func_jsons": []})

#     subs = [p for p in bids_root.glob("sub-*") if p.is_dir()]
#     if sub_filter:
#         subs = [p for p in subs if p.name in sub_filter]

#     for sub_path in subs:
#         ses_dirs = list(sub_path.glob("ses-*"))
#         ses_paths = ses_dirs if ses_dirs else [sub_path]

#         if ses_filter and ses_dirs:
#             ses_paths = [p for p in ses_paths if p.name in ses_filter]

#         for ses_path in ses_paths:
#             key = (sub_path.name, ses_path.name if ses_dirs else None)
#             for j in ses_path.glob("fmap/*_epi.json"):
#                 result[key]["fmap_jsons"].append(j)
#             for j in ses_path.glob("func/*_bold.json"):
#                 result[key]["func_jsons"].append(j)

#     return result

# # ---------------- assignment ----------------

# def choose_nearest_fmap(func_meta, fmap_candidates):
#     func_time = seconds_of_day(func_meta.get("AcquisitionTime"))
#     func_series = func_meta.get("SeriesNumber")
#     best = None
#     best_key = (float("inf"), float("inf"))
#     for fm in fmap_candidates:
#         fm_time = seconds_of_day(fm["meta"].get("AcquisitionTime"))
#         fm_series = fm["meta"].get("SeriesNumber")
#         dt = abs(func_time - fm_time) if (func_time is not None and fm_time is not None) else float("inf")
#         ds = abs(int(func_series) - int(fm_series)) if (func_series is not None and fm_series is not None) else float("inf")
#         key = (dt, ds)
#         if key < best_key:
#             best_key = key
#             best = fm
#     return best

# # ---- NEW: subject-relative IntendedFor path ----

# def drop_subject_prefix(path_str: str) -> str:
#     """
#     Remove leading 'sub-.../' from a POSIX path string if present.
#     """
#     if path_str.startswith("sub-") and "/" in path_str:
#         return path_str.split("/", 1)[1]
#     return path_str

# def intended_rel_without_sub(nii_path: Path, bids_root: Path) -> str:
#     """
#     Return subject-relative path like 'ses-7T2/func/...nii.gz' (i.e., no leading 'sub-.../').
#     """
#     try:
#         rel = nii_path.relative_to(bids_root).as_posix()
#     except Exception:
#         rel = nii_path.as_posix()
#     return drop_subject_prefix(rel)

# # ---------------- main ----------------

# def main():
#     ap = argparse.ArgumentParser()
#     ap.add_argument("bids_root", type=Path, help="Path to BIDS root (directory containing sub-*/)")
#     ap.add_argument("--write", action="store_true", help="Write IntendedFor into fmap jsons (default: dry-run)")
#     ap.add_argument("--dry-run", action="store_true", help="Print mapping only (default if --write not given)")
#     ap.add_argument("--sub", dest="subs", action="append", help="Limit to specific subject(s): 'sub-01' or path to sub-01")
#     ap.add_argument("--ses", dest="sess", action="append", help="Limit to specific session(s): 'ses-7T1' or path to ses-7T1")
#     args = ap.parse_args()

#     if not args.write:
#         args.dry_run = True

#     root = args.bids_root
#     if not root.exists():
#         print(f"[ERROR] {root} does not exist", file=sys.stderr)
#         sys.exit(1)

#     sub_filter = _normalize_label_items(args.subs, "sub-")
#     ses_filter = _normalize_label_items(args.sess, "ses-")

#     inventory = find_files(root, sub_filter=sub_filter, ses_filter=ses_filter)

#     total_links = 0
#     for (sub, ses), lists in sorted(inventory.items()):
#         if not lists["fmap_jsons"]:
#             continue

#         hdr = f"{sub} {ses or ''}".strip()
#         print(f"\n=== {hdr} ===")

#         fmap_entries = []
#         for jpath in sorted(lists["fmap_jsons"]):
#             meta = read_json(jpath)
#             nifti = jpath.with_suffix("").with_suffix(".nii.gz")
#             fmap_entries.append({"json": jpath, "nii": nifti, "meta": meta})

#         func_entries = []
#         for jpath in sorted(lists["func_jsons"]):
#             meta = read_json(jpath)
#             nifti = jpath.with_suffix("").with_suffix(".nii.gz")
#             func_entries.append({"json": jpath, "nii": nifti, "meta": meta})

#         # Group echoes by run key
#         func_groups = defaultdict(list)
#         for fe in func_entries:
#             key = strip_echo(fe["json"].name)
#             func_groups[key].append(fe)

#         fmap_by_dir = defaultdict(list)
#         for fm in fmap_entries:
#             pe = fm["meta"].get("PhaseEncodingDirection")
#             if pe:
#                 fmap_by_dir[pe].append(fm)

#         fmap_to_intended = defaultdict(set)

#         for gkey, group_files in sorted(func_groups.items()):
#             rep = sorted(group_files, key=lambda x: x["meta"].get("EchoNumber", 0))[0]
#             func_pe = rep["meta"].get("PhaseEncodingDirection")
#             want_pe = opposite_pe(func_pe) if func_pe else None

#             candidates = []
#             if want_pe and want_pe in fmap_by_dir:
#                 for fm in fmap_by_dir[want_pe]:
#                     if geom_compatible(fm["meta"], rep["meta"]):
#                         candidates.append(fm)

#             if not candidates and want_pe and want_pe in fmap_by_dir:
#                 candidates = fmap_by_dir[want_pe][:]

#             if not candidates:
#                 candidates = fmap_entries[:]

#             chosen = choose_nearest_fmap(rep["meta"], candidates)
#             if not chosen:
#                 print(f"[WARN] No fmap choice for run {gkey} (func {rep['json'].name})")
#                 continue

#             for fe in sorted(group_files, key=lambda x: x["meta"].get("EchoNumber", 0)):
#                 rel_no_sub = intended_rel_without_sub(fe["nii"], root)
#                 fmap_to_intended[chosen["json"]].add(rel_no_sub)

#             ft = seconds_of_day(rep["meta"].get("AcquisitionTime"))
#             ct = seconds_of_day(chosen["meta"].get("AcquisitionTime"))
#             dt_str = f" (dt={abs(ft-ct):.1f}s)" if (ft is not None and ct is not None) else ""
#             print(f"Map {gkey} -> {chosen['json'].name}{dt_str}")

#         # Write / show
#         for fmap_json, targets in sorted(fmap_to_intended.items(), key=lambda x: x[0].name):
#             meta = read_json(fmap_json)
#             new_list = sorted(targets)
#             meta["IntendedFor"] = new_list
#             total_links += len(new_list)

#             print(f"\n[fmap] {fmap_json.relative_to(root)}")
#             for t in new_list:
#                 print(f"  - {t}")

#             if args.write:
#                 write_json(fmap_json, meta, backup=True)

#     print(f"\nDone. {'Wrote' if not args.dry_run else 'Planned'} {total_links} IntendedFor links.")

# if __name__ == "__main__":
#     main()

# #!/usr/bin/env python3
# """
# Assign IntendedFor for reverse-polarity EPI fieldmaps to matching functional runs.

# Strategy
# - Walk BIDS-like tree under ROOT (optionally restricted to one subject/session).
# - Identify reverse-polarity EPI series (by folder='fmap' with suffix 'epi'
#   or by SeriesDescription/ProtocolName containing 'rev', 'reverse', 'polarity', 'topup', 'blip').
# - Identify functional BOLD runs (including multi-echo); group echoes by run.
# - Build a "sequence fingerprint" from the JSON (matrix, MB factor, TR, echo spacing, readout time,
#   orientation, slice timing length, voxel spacing, etc.).
# - For each session, pair each rev-PE EPI with functional runs that have:
#     * same fingerprint, and
#     * opposite PhaseEncodingDirection sign (e.g. j  <-> j-)
#   If multiple rev-PE EPIs exist with the same fingerprint, use AcquisitionTime midpoints
#   to assign the nearest-in-time subset of runs to each EPI.
# - Write/update IntendedFor in the EPI JSON sidecars.
# - IntendedFor paths are POSIX and relative to the subject dir (e.g., "ses-7T2/func/...").

# Usage examples
#   python IntendedFor_JSBR.py /path/to/BIDSroot
#   python IntendedFor_JSBR.py --sub SUB001 /path/to/BIDSroot
#   python IntendedFor_JSBR.py --sess 7T2 /path/to/BIDSroot
#   python IntendedFor_JSBR.py --ses /.../sub-SUB001/ses-7T2 /path/to/BIDSroot
# """

# from __future__ import annotations
# import argparse
# import json
# import re
# import sys
# from pathlib import Path
# from typing import Dict, List, Optional, Tuple, Any


# # --------------------------- small helpers ---------------------------

# def load_json(p: Path) -> Dict[str, Any]:
#     with p.open("r") as f:
#         return json.load(f)


# def save_json(p: Path, obj: Dict[str, Any]) -> None:
#     with p.open("w") as f:
#         json.dump(obj, f, indent=4, sort_keys=False)
#         f.write("\n")


# def as_posix(p: Path) -> str:
#     return p.as_posix()


# def parse_acq_time(s: Optional[str]) -> Optional[float]:
#     """
#     Parse 'HH:MM:SS[.ffffff]' into seconds from midnight (float).
#     Returns None if s is falsy or malformed.
#     """
#     if not s or not isinstance(s, str):
#         return None
#     try:
#         hh, mm, ss = s.split(":")
#         sec = float(ss)
#         return int(hh) * 3600 + int(mm) * 60 + sec
#     except Exception:
#         return None


# def ped_invert(pe: Optional[str]) -> Optional[str]:
#     """Invert PhaseEncodingDirection sign: 'i' <-> 'i-', 'j' <-> 'j-', 'k' <-> 'k-'."""
#     if pe is None:
#         return None
#     if pe.endswith("-"):
#         return pe[:-1]
#     return pe + "-"


# def ped_axis(pe: Optional[str]) -> Optional[str]:
#     """Return axis letter only: 'i'|'j'|'k' for 'i','i-','j','j-','k','k-'."""
#     if pe is None:
#         return None
#     return pe.replace("-", "")


# def round_list(vals: List[float], nd=3) -> List[float]:
#     try:
#         return [round(float(v), nd) for v in vals]
#     except Exception:
#         return []


# def intended_relpath(p: Path, bids_root: Path, sub_name: str) -> str:
#     """
#     Return POSIX path for IntendedFor relative to the subject dir.
#     Drops leading 'sub-XXX/' if present.
#     """
#     try:
#         rel = p.relative_to(bids_root)
#     except ValueError:
#         rel = p  # fallback
#     parts = list(rel.parts)
#     if parts and parts[0] == sub_name:
#         parts = parts[1:]
#     return "/".join(parts)


# # --------------------------- identification ---------------------------

# REV_MARKERS = re.compile(r"(rev|reverse|polarity|topup|blip)", re.I)

# def is_rev_epi(json_meta: Dict[str, Any], json_path: Path) -> bool:
#     """
#     Heuristics to treat a series as a reverse-polarity EPI 'fieldmap':
#       - If in 'fmap' directory and suffix likely 'epi' -> True
#       - Else, if SeriesDescription or ProtocolName contains rev/blip/topup/polarity -> True
#     """
#     # In fmap folder -> likely a fieldmap (epi, or phasediff/magnitude). Prefer epi.
#     if json_path.parent.name == "fmap":
#         # If filename looks like *_epi.json, assume it's a pepolar epi.
#         if json_path.name.endswith("_epi.json"):
#             return True
#         # Still allow other names in fmap as rev epi if markers present.
#         sd = str(json_meta.get("SeriesDescription", "")) + " " + str(json_meta.get("ProtocolName", ""))
#         if REV_MARKERS.search(sd):
#             return True
#         # otherwise, be conservative
#         return False

#     # Else allow func-placed rev polarity acquisitions (common at some sites)
#     sd = str(json_meta.get("SeriesDescription", "")) + " " + str(json_meta.get("ProtocolName", ""))
#     return bool(REV_MARKERS.search(sd))


# def is_func_bold(json_meta: Dict[str, Any], json_path: Path) -> bool:
#     """
#     Identify functional BOLD runs (including multi-echo).
#     Exclude obvious rev/topup series and anything in fmap folder.
#     """
#     if json_path.parent.name == "fmap":
#         return False
#     sd = str(json_meta.get("SeriesDescription", "")) + " " + str(json_meta.get("ProtocolName", ""))
#     if REV_MARKERS.search(sd):
#         return False
#     # Heuristic: expect BOLD EPIs; allow anything in 'func' or with BOLD-like tags
#     if json_path.parent.name == "func":
#         return True
#     # Fallback: accept if it looks like BOLD by metadata
#     return "cmrr_mbep2d_bold" in sd.lower() or "bold" in sd.lower()


# # --------------------------- fingerprinting ---------------------------

# def seq_fingerprint(meta: Dict[str, Any]) -> Tuple:
#     """
#     Build a fingerprint to match rev-PE EPI with func runs.
#     Use robust, stable fields that should match exactly if geometries are identical.
#     Note: Do NOT include PE sign; include only the axis ('i'/'j'/'k').
#     """
#     axis = ped_axis(meta.get("PhaseEncodingDirection"))
#     # Orientation rounded to reduce tiny numeric diffs
#     orient = round_list(meta.get("ImageOrientationPatientDICOM", []) or meta.get("ImageOrientation", []), nd=3)

#     return (
#         meta.get("BaseResolution", None),
#         meta.get("AcquisitionMatrixPE", None),
#         meta.get("ReconMatrixPE", None),
#         meta.get("MultibandAccelerationFactor", None),
#         meta.get("ParallelReductionFactorInPlane", None),
#         meta.get("TotalReadoutTime", None),
#         meta.get("EffectiveEchoSpacing", None),
#         meta.get("RepetitionTime", None),
#         meta.get("SliceThickness", None),
#         meta.get("SpacingBetweenSlices", None),
#         len(meta.get("SliceTiming", []) or []),
#         tuple(orient) if orient else None,
#         axis,
#         # Also include FOV percent to catch edge cases
#         meta.get("PercentPhaseFOV", None),
#         meta.get("PercentSampling", None),
#         meta.get("EchoTrainLength", None),
#         meta.get("BandwidthPerPixelPhaseEncode", None),
#         meta.get("PixelBandwidth", None),
#     )


# # --------------------------- inventory collection ---------------------------

# def collect_session_inventory(ses_path: Path) -> Dict[str, List[Dict[str, Any]]]:
#     """
#     Return dict with keys: 'rev', 'func_groups'
#       - 'rev' is a list of rev-PE EPI entries
#       - 'func_groups' is a list of grouped multi-echo runs (one record per run with all echoes)
#     """
#     out_rev: List[Dict[str, Any]] = []
#     func_entries: List[Dict[str, Any]] = []

#     # Search JSONs under session
#     for json_path in ses_path.rglob("*.json"):
#         # restrict to common locations
#         if json_path.parent.name not in ("func", "fmap"):
#             continue
#         nii_path = json_path.with_suffix("").with_suffix(".nii.gz")
#         if not nii_path.exists():
#             # some converters produce JSON without image; skip
#             continue

#         meta = load_json(json_path)

#         if is_rev_epi(meta, json_path):
#             out_rev.append({
#                 "json": json_path,
#                 "nii": nii_path,
#                 "meta": meta,
#                 "sub": guess_sub(ses_path),
#                 "ses": ses_path.name if ses_path.name.startswith("ses-") else None,
#                 "acq_time": parse_acq_time(meta.get("AcquisitionTime")),
#                 "pe": meta.get("PhaseEncodingDirection"),
#                 "axis": ped_axis(meta.get("PhaseEncodingDirection")),
#                 "fp": seq_fingerprint(meta),
#             })
#         elif is_func_bold(meta, json_path):
#             func_entries.append({
#                 "json": json_path,
#                 "nii": nii_path,
#                 "meta": meta,
#                 "sub": guess_sub(ses_path),
#                 "ses": ses_path.name if ses_path.name.startswith("ses-") else None,
#                 "acq_time": parse_acq_time(meta.get("AcquisitionTime")),
#                 "pe": meta.get("PhaseEncodingDirection"),
#                 "axis": ped_axis(meta.get("PhaseEncodingDirection")),
#                 "fp": seq_fingerprint(meta),
#             })

#     # Group functional echoes into runs by filename with echo stripped
#     groups = group_func_runs(func_entries)

#     return {"rev": out_rev, "func_groups": groups}


# def guess_sub(ses_path: Path) -> Optional[str]:
#     """
#     Return 'sub-XXX' name for a session path.
#     """
#     for p in ses_path.parents if ses_path.is_dir() else ses_path.parents:
#         if p.name.startswith("sub-"):
#             return p.name
#     # If ses_path itself is sub-XXX (no 'ses-*' layer)
#     if ses_path.name.startswith("sub-"):
#         return ses_path.name
#     return None


# RUN_RE = re.compile(r"(.*?)(?:_echo-\d+)(.*)")

# def strip_echo_tag(name: str) -> str:
#     """
#     Remove `_echo-<num>` from a BOLD filename stem to build the run key.
#     """
#     return RUN_RE.sub(r"\1\2", name)


# def group_func_runs(func_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
#     """
#     Group multi-echo runs: return one record per run containing all echoes.
#     """
#     by_key: Dict[str, Dict[str, Any]] = {}
#     for fe in func_entries:
#         stem = fe["nii"].name.replace(".nii.gz", "")
#         key = strip_echo_tag(stem)
#         rec = by_key.get(key)
#         if rec is None:
#             # initialize group using TE1 metadata when available
#             by_key[key] = {
#                 "key": key,
#                 "sub": fe["sub"],
#                 "ses": fe["ses"],
#                 "echoes": [],
#                 "acq_time": fe["acq_time"],
#                 "pe": fe["pe"],
#                 "axis": fe["axis"],
#                 "fp": fe["fp"],
#             }
#         # prefer TE1 as representative for time/metadata if available
#         echo_num = fe["meta"].get("EchoNumber")
#         if echo_num == 1 or by_key[key]["acq_time"] is None:
#             by_key[key]["acq_time"] = fe["acq_time"]
#             by_key[key]["pe"] = fe["pe"]
#             by_key[key]["axis"] = fe["axis"]
#             by_key[key]["fp"] = fe["fp"]
#         by_key[key]["echoes"].append(fe)

#     # sort echoes within each run by EchoNumber if present
#     groups = list(by_key.values())
#     for g in groups:
#         g["echoes"].sort(key=lambda e: (e["meta"].get("EchoNumber", 999), e["nii"].name))
#     # stable order by acquisition time, then key
#     groups.sort(key=lambda g: (g["acq_time"] if g["acq_time"] is not None else 9e9, g["key"]))
#     return groups


# # --------------------------- pairing logic ---------------------------

# def midpoint(a: float, b: float) -> float:
#     return (a + b) / 2.0


# def assign_by_time_windows(fmaps: List[Dict[str, Any]],
#                            funcs: List[Dict[str, Any]]) -> Dict[Path, List[Path]]:
#     """
#     Given fmaps and funcs already filtered to share fingerprint and opposite PE sign,
#     split funcs to the nearest-in-time fieldmap by using midpoints between fmap acquisition times.
#     If any fmap or func lacks acq_time, fall back to assigning all funcs to all fmaps.
#     """
#     # If any time is None, simple "assign all" fallback
#     if any(f["acq_time"] is None for f in fmaps) or any(g["acq_time"] is None for g in funcs):
#         res = {}
#         for f in fmaps:
#             res[f["json"]] = [e["nii"] for g in funcs for e in g["echoes"]]
#         return res

#     # sort by time
#     fmaps_sorted = sorted(fmaps, key=lambda f: f["acq_time"])
#     fmap_times = [f["acq_time"] for f in fmaps_sorted]

#     # Build windows for each fmap
#     left_bounds = []
#     right_bounds = []
#     for i, t in enumerate(fmap_times):
#         left = -1e9 if i == 0 else midpoint(fmap_times[i - 1], t)
#         right = 1e9 if i == len(fmap_times) - 1 else midpoint(t, fmap_times[i + 1])
#         left_bounds.append(left)
#         right_bounds.append(right)

#     # Assign funcs to windows
#     out: Dict[Path, List[Path]] = {f["json"]: [] for f in fmaps_sorted}
#     for g in funcs:
#         t = g["acq_time"]
#         if t is None:
#             # shouldn't happen due to check above
#             targets = [f["json"] for f in fmaps_sorted]
#         else:
#             targets = []
#             for i, f in enumerate(fmaps_sorted):
#                 if left_bounds[i] <= t < right_bounds[i]:
#                     targets = [f["json"]]
#                     break
#             if not targets:
#                 # fallback: nearest single fmap
#                 closest_idx = min(range(len(fmaps_sorted)), key=lambda i: abs(fmap_times[i] - t))
#                 targets = [fmaps_sorted[closest_idx]["json"]]
#         for tgt in targets:
#             out[tgt].extend(e["nii"] for e in g["echoes"])

#     return out


# def build_intendedfor_for_session(bids_root: Path, inv: Dict[str, List[Dict[str, Any]]]) -> Dict[Path, List[str]]:
#     """
#     Return mapping: fmap_json_path -> [IntendedFor POSIX paths relative to subject dir]
#     """
#     # Group by (fingerprint-with-axis, sub, ses)
#     # For each group, split funcs by opposite PE sign and assign by time windows
#     fmap_map: Dict[Path, List[str]] = {}

#     # Index funcs by (fp, axis, sub, ses)
#     func_index: Dict[Tuple, List[Dict[str, Any]]] = {}
#     for g in inv["func_groups"]:
#         key = (g["fp"], g["axis"], g["sub"], g["ses"])
#         func_index.setdefault(key, []).append(g)

#     # Consider each fmap and gather matching funcs (same fp/axis, opposite sign)
#     # We'll first group fmaps per (fp, axis, sub, ses) to allow time-based splitting
#     fmap_groups: Dict[Tuple, List[Dict[str, Any]]] = {}
#     for f in inv["rev"]:
#         key = (f["fp"], f["axis"], f["sub"], f["ses"])
#         fmap_groups.setdefault(key, []).append(f)

#     for key, fmap_list in fmap_groups.items():
#         fp, axis, sub, ses = key
#         # Opposite-PE funcs live under same fp/axis/sub/ses but with PE sign inverted.
#         # Since axis is the same in fp, we only need funcs with same fp & axis.
#         funcs = func_index.get(key, [])
#         if not funcs:
#             # no funcs with matching fingerprint+axis; skip
#             continue

#         # Split funcs by PE sign relative to fmap sign:
#         # If fmap PE is 'j', we want funcs that are 'j-'; and vice versa.
#         fmap_pe_sign = inv["rev"][0]["meta"].get("PhaseEncodingDirection")  # default, not used directly
#         # But better derive desired func sign from any one fmap in the list:
#         desired_func_pe = ped_invert(fmap_list[0]["pe"])

#         funcs_opposite: List[Dict[str, Any]] = []
#         for g in funcs:
#             if g["pe"] == desired_func_pe:
#                 funcs_opposite.append(g)

#         if not funcs_opposite:
#             # If no exact sign match, fall back to all funcs of this fingerprint/axis
#             funcs_opposite = funcs

#         # Assign by time windows within this fp/axis/sub/ses bucket
#         assignment = assign_by_time_windows(fmap_list, funcs_opposite)

#         # Convert to POSIX IntendedFor strings relative to subject dir
#         sub_name = sub or guess_sub(Path(ses) if ses else Path("."))
#         for fmap_json, nii_list in assignment.items():
#             rels = [
#                 intended_relpath(nii, bids_root, sub_name)  # <-- subject-relative
#                 for nii in sorted(set(nii_list))
#             ]
#             fmap_map[fmap_json] = sorted(set(rels))

#     return fmap_map


# # --------------------------- scanning and CLI ---------------------------

# def find_sessions(root: Path,
#                   sub_filter: Optional[List[str]] = None,
#                   ses_filter: Optional[List[str]] = None,
#                   ses_path_hint: Optional[str] = None) -> List[Path]:
#     """
#     Collect session paths under root honoring optional filters or a session path hint.
#     - sub_filter: list of 'SUB001' style names (without 'sub-')
#     - ses_filter: list of '7T2' style names (without 'ses-')
#     - ses_path_hint: a path like '/.../sub-XXX/ses-YYY' to restrict to one session
#     """
#     # If a session-path hint was provided, extract sub/ses and build a single target
#     if ses_path_hint:
#         p = Path(ses_path_hint)
#         if p.exists():
#             # Expect .../sub-XXX/ses-YYY[/...]
#             sub_dir = None
#             ses_dir = None
#             for part in p.parts:
#                 if part.startswith("sub-"):
#                     sub_dir = part
#                 if part.startswith("ses-"):
#                     ses_dir = part
#             # Fallbacks if not found
#             if sub_dir is None:
#                 # try to get from parent hierarchy under provided root
#                 for cand in p.parents:
#                     if cand.name.startswith("sub-"):
#                         sub_dir = cand.name
#                         break
#             if ses_dir is None:
#                 for cand in p.parents:
#                     if cand.name.startswith("ses-"):
#                         ses_dir = cand.name
#                         break

#             if sub_dir and ses_dir:
#                 ses_full = root / sub_dir / ses_dir
#                 if ses_full.exists():
#                     return [ses_full]

#     # Otherwise, enumerate normally
#     if sub_filter:
#         subs = [root / f"sub-{s}" for s in sub_filter]
#     else:
#         subs = [p for p in root.glob("sub-*") if p.is_dir()]

#     sessions: List[Path] = []
#     for sub_path in subs:
#         ses_paths = list(sub_path.glob("ses-*")) or [sub_path]
#         if ses_filter:
#             ses_paths = [p for p in ses_paths if p.name.split("-")[-1] in set(ses_filter)]
#         sessions.extend(ses_paths)

#     # Keep sessions that actually have func or fmap content
#     filt = []
#     for s in sessions:
#         if (s / "func").exists() or (s / "fmap").exists():
#             filt.append(s)
#     return filt


# def apply_intendedfor(root: Path,
#                       fmap_map: Dict[Path, List[str]],
#                       dry_run: bool = False) -> None:
#     """
#     Write IntendedFor arrays into the EPI JSONs.
#     """
#     if not fmap_map:
#         print("No fieldmaps to update.")
#         return

#     for json_path, rels in fmap_map.items():
#         try:
#             meta = load_json(json_path)
#         except Exception as e:
#             print(f"[WARN] Could not read JSON {json_path}: {e}")
#             continue

#         before = meta.get("IntendedFor", [])
#         if before != rels:
#             print(f"[UPDATE] {json_path.relative_to(root)}")
#             print(f"         IntendedFor: {len(before)} -> {len(rels)} entries")
#             if not dry_run:
#                 meta["IntendedFor"] = rels
#                 save_json(json_path, meta)
#         else:
#             print(f"[SKIP]   {json_path.relative_to(root)} (no change)")


# def main():
#     ap = argparse.ArgumentParser(description="Populate IntendedFor for reverse-PE EPIs.")
#     ap.add_argument("root", type=Path, help="BIDS root directory")
#     ap.add_argument("--sub", "--subs", dest="subs", nargs="+", help="Subject(s) without 'sub-' prefix, e.g. SUB001")
#     ap.add_argument("--ses", "--sess", dest="sess", nargs="*", help="Session(s) without 'ses-' prefix, or a single PATH to a specific ses dir")
#     ap.add_argument("--dry-run", action="store_true", help="Do not write files, just show planned changes")
#     args = ap.parse_args()

#     root = args.root.resolve()

#     # Interpret --ses: it can be a list of names (7T1 7T2) OR a single path to a session
#     ses_path_hint = None
#     ses_filter = None
#     if args.sess:
#         if len(args.sess) == 1 and (Path(args.sess[0]).exists()):
#             ses_path_hint = args.sess[0]
#         else:
#             ses_filter = args.sess

#     sessions = find_sessions(root,
#                              sub_filter=args.subs,
#                              ses_filter=ses_filter,
#                              ses_path_hint=ses_path_hint)

#     if not sessions:
#         print("No sessions found matching filters.", file=sys.stderr)
#         sys.exit(1)

#     total_updates = 0
#     for ses_path in sessions:
#         print(f"\n== Session: {ses_path.relative_to(root)} ==")
#         inv = collect_session_inventory(ses_path)
#         if not inv["rev"]:
#             print("  (no reverse-PE EPI fieldmaps found; skipping)")
#             continue
#         if not inv["func_groups"]:
#             print("  (no functional runs found; skipping)")
#             continue

#         fmap_map = build_intendedfor_for_session(root, inv)
#         if not fmap_map:
#             print("  (no matching funcs for the found fieldmaps; skipping)")
#             continue

#         apply_intendedfor(root, fmap_map, dry_run=args.dry_run)
#         total_updates += len(fmap_map)

#     if total_updates == 0:
#         print("\nDone. No updates.")
#     else:
#         print(f"\nDone. Updated {total_updates} fieldmap JSON(s).")


# if __name__ == "__main__":
#     main()





# #!/usr/bin/env python3
# """
# IntendedFor_JSBR.py

# Populate fmap/*.json "IntendedFor" by assigning each functional run (grouped across echoes)
# to the closest-in-time, opposite-PE EPI fieldmap in the same session, requiring compatible
# geometry/readout. Robust to being given --sub/--ses as labels (sub-01, ses-02) or as full paths.

# Usage examples:
#   # dry run
#   python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses ses-7T1
#   # write changes
#   python IntendedFor_JSBR.py /path/to/bidsroot --sub sub-01 --ses /path/to/bidsroot/sub-01/ses-7T1 --write
# """

# import argparse
# import json
# import re
# import sys
# from pathlib import Path
# from datetime import datetime
# from collections import defaultdict

# FLOAT_TOL = 1e-6

# def read_json(p: Path):
#     try:
#         with p.open("r") as f:
#             return json.load(f)
#     except Exception as e:
#         print(f"[WARN] Failed to read {p}: {e}", file=sys.stderr)
#         return {}

# def write_json(p: Path, obj, backup=True):
#     try:
#         if backup and p.exists():
#             bak = p.with_suffix(p.suffix + ".bak")
#             bak.write_text(p.read_text())
#         with p.open("w") as f:
#             json.dump(obj, f, indent=4)
#             f.write("\n")
#     except Exception as e:
#         print(f"[ERROR] Failed to write {p}: {e}", file=sys.stderr)

# def seconds_of_day(acq_time: str):
#     if not acq_time:
#         return None
#     try:
#         if "." in acq_time:
#             base, frac = acq_time.split(".")
#             frac = (frac + "000000")[:6]
#             acq_time = f"{base}.{frac}"
#             t = datetime.strptime(acq_time, "%H:%M:%S.%f")
#         else:
#             t = datetime.strptime(acq_time, "%H:%M:%S")
#         return t.hour * 3600 + t.minute * 60 + t.second + (t.microsecond / 1e6)
#     except Exception:
#         return None

# def strip_echo(basename: str) -> str:
#     return re.sub(r"_echo-\d+\b", "", basename)

# def opposite_pe(pe: str):
#     if not pe:
#         return None
#     return pe[:-1] if pe.endswith("-") else pe + "-"

# def approx_equal(a, b, tol=FLOAT_TOL):
#     try:
#         return abs(float(a) - float(b)) <= tol
#     except Exception:
#         return a == b

# def listify(x):
#     if x is None:
#         return []
#     return x if isinstance(x, list) else [x]

# def geom_signature(meta: dict):
#     return {
#         "BaseResolution": meta.get("BaseResolution"),
#         "AcquisitionMatrixPE": meta.get("AcquisitionMatrixPE"),
#         "ReconMatrixPE": meta.get("ReconMatrixPE"),
#         "TotalReadoutTime": meta.get("TotalReadoutTime"),
#         "EffectiveEchoSpacing": meta.get("EffectiveEchoSpacing"),
#         "ParallelReductionFactorInPlane": meta.get("ParallelReductionFactorInPlane"),
#         "MultibandAccelerationFactor": meta.get("MultibandAccelerationFactor"),
#         "SliceThickness": meta.get("SliceThickness"),
#         "SpacingBetweenSlices": meta.get("SpacingBetweenSlices"),
#         "ImageOrientationPatientDICOM": meta.get("ImageOrientationPatientDICOM"),
#         "PhaseEncodingDirection": meta.get("PhaseEncodingDirection"),
#     }

# def geom_compatible(fmap_meta: dict, func_meta: dict) -> bool:
#     A = geom_signature(fmap_meta)
#     B = geom_signature(func_meta)

#     if A.get("PhaseEncodingDirection") and B.get("PhaseEncodingDirection"):
#         if A["PhaseEncodingDirection"] != opposite_pe(B["PhaseEncodingDirection"]):
#             return False

#     scalar_keys = [
#         "BaseResolution",
#         "AcquisitionMatrixPE",
#         "ReconMatrixPE",
#         "TotalReadoutTime",
#         "EffectiveEchoSpacing",
#         "ParallelReductionFactorInPlane",
#         "MultibandAccelerationFactor",
#         "SliceThickness",
#         "SpacingBetweenSlices",
#     ]
#     for k in scalar_keys:
#         av = A.get(k); bv = B.get(k)
#         if av is None or bv is None:
#             continue
#         tol = 1e-5 if "Time" in k or "Spacing" in k else 1e-3
#         if not approx_equal(av, bv, tol=tol):
#             return False

#     a_ori = listify(A.get("ImageOrientationPatientDICOM"))
#     b_ori = listify(B.get("ImageOrientationPatientDICOM"))
#     if a_ori and b_ori:
#         if len(a_ori) != len(b_ori):
#             return False
#         for av, bv in zip(a_ori, b_ori):
#             if not approx_equal(av, bv, tol=1e-4):
#                 return False

#     return True

# def _normalize_label_items(items, prefix):
#     """
#     Accept entries like 'ses-7T1', '7T1', '/path/.../ses-7T1', and return a set of canonical names.
#     For subs, prefix='sub-'; for sessions, prefix='ses-'.
#     """
#     if not items:
#         return None
#     out = set()
#     for it in items:
#         name = it
#         try:
#             p = Path(it)
#             # Try to extract part that starts with the prefix from a path
#             parts = [part for part in p.parts if part.startswith(prefix)]
#             if parts:
#                 name = parts[-1]
#             else:
#                 name = p.name  # last component
#         except Exception:
#             pass
#         if not name.startswith(prefix):
#             name = f"{prefix}{name}"
#         out.add(name)
#     return out or None

# def find_files(bids_root: Path, sub_filter=None, ses_filter=None):
#     """
#     Return dict keyed by (sub, ses) with lists of fmap jsons and func jsons.
#     - sub_filter/ses_filter are sets like {'sub-01', ...} / {'ses-7T1', ...}
#     """
#     result = defaultdict(lambda: {"fmap_jsons": [], "func_jsons": []})

#     subs = [p for p in bids_root.glob("sub-*") if p.is_dir()]
#     if sub_filter:
#         subs = [p for p in subs if p.name in sub_filter]

#     for sub_path in subs:
#         ses_dirs = list(sub_path.glob("ses-*"))
#         ses_paths = ses_dirs if ses_dirs else [sub_path]  # subject-level (no sessions)

#         if ses_filter and ses_dirs:
#             ses_paths = [p for p in ses_paths if p.name in ses_filter]

#         for ses_path in ses_paths:
#             key = (sub_path.name, ses_path.name if ses_dirs else None)

#             for j in ses_path.glob("fmap/*_epi.json"):
#                 result[key]["fmap_jsons"].append(j)

#             for j in ses_path.glob("func/*_bold.json"):
#                 result[key]["func_jsons"].append(j)

#     return result

# def choose_nearest_fmap(func_meta, fmap_candidates):
#     func_time = seconds_of_day(func_meta.get("AcquisitionTime"))
#     func_series = func_meta.get("SeriesNumber")
#     best = None
#     best_key = (float("inf"), float("inf"))
#     for fm in fmap_candidates:
#         fm_time = seconds_of_day(fm["meta"].get("AcquisitionTime"))
#         fm_series = fm["meta"].get("SeriesNumber")
#         dt = abs(func_time - fm_time) if (func_time is not None and fm_time is not None) else float("inf")
#         ds = abs(int(func_series) - int(fm_series)) if (func_series is not None and fm_series is not None) else float("inf")
#         key = (dt, ds)
#         if key < best_key:
#             best_key = key
#             best = fm
#     return best

# def main():
#     ap = argparse.ArgumentParser()
#     ap.add_argument("bids_root", type=Path, help="Path to BIDS root (directory containing sub-*/)")
#     ap.add_argument("--write", action="store_true", help="Write IntendedFor into fmap jsons (default: dry-run)")
#     ap.add_argument("--dry-run", action="store_true", help="Print mapping only (default if --write not given)")
#     ap.add_argument("--sub", dest="subs", action="append", help="Limit to specific subject(s): 'sub-01' or path to sub-01")
#     ap.add_argument("--ses", dest="sess", action="append", help="Limit to specific session(s): 'ses-7T1' or path to ses-7T1")
#     args = ap.parse_args()

#     if not args.write:
#         args.dry_run = True

#     root = args.bids_root
#     if not root.exists():
#         print(f"[ERROR] {root} does not exist", file=sys.stderr)
#         sys.exit(1)

#     sub_filter = _normalize_label_items(args.subs, "sub-")
#     ses_filter = _normalize_label_items(args.sess, "ses-")

#     inventory = find_files(root, sub_filter=sub_filter, ses_filter=ses_filter)

#     total_links = 0
#     for (sub, ses), lists in sorted(inventory.items()):
#         if not lists["fmap_jsons"]:
#             continue

#         hdr = f"{sub} {ses or ''}".strip()
#         print(f"\n=== {hdr} ===")

#         fmap_entries = []
#         for jpath in sorted(lists["fmap_jsons"]):
#             meta = read_json(jpath)
#             nifti = jpath.with_suffix("").with_suffix(".nii.gz")
#             fmap_entries.append({"json": jpath, "nii": nifti, "meta": meta})

#         func_entries = []
#         for jpath in sorted(lists["func_jsons"]):
#             meta = read_json(jpath)
#             nifti = jpath.with_suffix("").with_suffix(".nii.gz")
#             func_entries.append({"json": jpath, "nii": nifti, "meta": meta})

#         func_groups = defaultdict(list)
#         for fe in func_entries:
#             key = strip_echo(fe["json"].name)
#             func_groups[key].append(fe)

#         fmap_by_dir = defaultdict(list)
#         for fm in fmap_entries:
#             pe = fm["meta"].get("PhaseEncodingDirection")
#             if pe:
#                 fmap_by_dir[pe].append(fm)

#         fmap_to_intended = defaultdict(set)

#         for gkey, group_files in sorted(func_groups.items()):
#             rep = sorted(group_files, key=lambda x: x["meta"].get("EchoNumber", 0))[0]
#             func_pe = rep["meta"].get("PhaseEncodingDirection")
#             want_pe = opposite_pe(func_pe) if func_pe else None

#             candidates = []
#             if want_pe and want_pe in fmap_by_dir:
#                 for fm in fmap_by_dir[want_pe]:
#                     if geom_compatible(fm["meta"], rep["meta"]):
#                         candidates.append(fm)

#             if not candidates and want_pe and want_pe in fmap_by_dir:
#                 candidates = fmap_by_dir[want_pe][:]

#             if not candidates:
#                 candidates = fmap_entries[:]

#             chosen = choose_nearest_fmap(rep["meta"], candidates)
#             if not chosen:
#                 print(f"[WARN] No fmap choice for run {gkey} (func {rep['json'].name})")
#                 continue

#             for fe in sorted(group_files, key=lambda x: x["meta"].get("EchoNumber", 0)):
#                 rel = fe["nii"].relative_to(root).as_posix()
#                 fmap_to_intended[chosen["json"]].add(rel)

#             ft = seconds_of_day(rep["meta"].get("AcquisitionTime"))
#             ct = seconds_of_day(chosen["meta"].get("AcquisitionTime"))
#             dt_str = f" (t={abs(ft-ct):.1f}s)" if (ft is not None and ct is not None) else ""
#             print(f"Map {gkey} -> {chosen['json'].name}{dt_str}")

#         for fmap_json, targets in sorted(fmap_to_intended.items(), key=lambda x: x[0].name):
#             meta = read_json(fmap_json)
#             new_list = sorted(targets)
#             meta["IntendedFor"] = new_list
#             total_links += len(new_list)

#             print(f"\n[fmap] {fmap_json.relative_to(root)}")
#             for t in new_list:
#                 print(f"  - {t}")

#             if args.write:
#                 write_json(fmap_json, meta, backup=True)

#     print(f"\nDone. {'Wrote' if not args.dry_run else 'Planned'} {total_links} IntendedFor links.")

# if __name__ == "__main__":
#     main()