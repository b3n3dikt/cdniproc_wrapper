#!/usr/bin/env python3
"""
view_tsv.py - review and (optionally) edit a session summary TSV as a plain text file,
so nobody has to open LibreOffice.

The TSV (made by the lab's nii_init_gpt5.py) stays the single source of truth: it is what
tsv_to_json.py reads in step 02. This script only produces an easy-to-read, EDITABLE text
view of it and, when you ask, copies your text edits back into the TSV.

    summaries/sub-X_ses-01.tsv                    the real file (tab separated)
    summaries/sub-X_ses-01.view.txt               the text view you can edit
    summaries/.sub-X_ses-01.view_base.tsv         hidden snapshot of the TSV when the view was made

SAFE BY DESIGN - edits are merged, never overwritten.
  Only the cells you CHANGED in the text file (compared with the snapshot) are written into the TSV.
  Cells you did not touch keep whatever the TSV has, so a change made by hand in LibreOffice is
  never reverted. If the same cell was changed to different values in both files, nothing is
  written and the conflict is listed.

Commands
  make   TSV [--force] [--print]   write the view + snapshot from the TSV (refuses to overwrite a view
                                   that has unapplied edits unless --force)
  status TSV                       list edits waiting in the view and run the CHECKS as if they were applied
  apply  TSV                       write the pending edits into the TSV (a timestamped backup is kept)

Edit rules for view.txt
  * Change only the columns marked with * in the header: label, MP, acq, PEdir, inv, nEcho.
  * An empty value is written as a single dot  .   Do not leave a cell blank.
  * Do not add, remove or reorder lines. Lines starting with # are ignored. The other columns are
    read-only (a change there is reported and ignored). 'run' is recalculated by tsv_to_json.py.
"""
import argparse
import re
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

EDIT = ["label", "MP", "acq", "PEdir", "inv", "EchoNumber"]            # columns a user may change
# view columns, in order: (key, header)  (* = editable). SeriesDescription is last (may contain spaces)
COLS = [("row", "row"), ("SeriesNumber", "series"), ("label", "label*"), ("MP", "MP*"), ("acq", "acq*"),
        ("PEdir", "PEdir*"), ("inv", "inv*"), ("EchoNumber", "nEcho*"), ("run", "run"),
        ("consistent", "cons."), ("dims", "dims"), ("time", "time"), ("SeriesDescription", "SeriesDescription")]
FUNC = {"rest", "task"}
SKIP_WORDS = ("SBRef", "setter", "Localizer", "Scout", "RECON")        # normally left unlabelled on purpose
OK_MP = {"", "M", "P"}
OK_PE = {"", "AP", "PA", "LR", "RL", "IS", "SI"}


# ---------------------------------------------------------------- helpers
def norm(col, v):
    """compare cells in a form that ignores '4.0' vs '4'"""
    v = "" if v is None else str(v).strip()
    if col in ("EchoNumber", "inv", "runNum") and re.fullmatch(r"-?\d+\.0+", v):
        v = v.split(".")[0]
    return v


def show(v):
    return v if v != "" else "."


def unshow(v):
    return "" if v == "." else v


def paths(tsv):
    tsv = Path(tsv)
    return tsv, tsv.with_suffix(".view.txt"), tsv.with_name("." + tsv.stem + ".view_base.tsv")


def read_tsv(path):
    df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False)
    for c in ("SeriesDescription", "label"):
        if c not in df.columns:
            sys.exit(f"ERROR: {path} has no '{c}' column - is it a nii_init TSV?")
    for c in EDIT + ["SeriesNumber", "consistent"]:
        if c not in df.columns:
            df[c] = ""
    return df


def add_calc(df):
    """columns derived the same way tsv_to_json.py does it (run number) + display helpers"""
    d = df.copy()
    for c in EDIT:
        d[c] = d[c].map(lambda v, c=c: norm(c, v))
    run = d.groupby(["label", "EchoNumber", "MP", "PEdir", "acq", "inv"]).cumcount().add(1).astype(str)
    d["run"] = run.where(d["label"] != "", "")
    dims = [c for c in ("fsl_dim1", "fsl_dim2", "fsl_dim3", "fsl_dim4") if c in d.columns]
    d["dims"] = d[dims].agg("x".join, axis=1).where(d[dims].ne("").all(axis=1), "") if dims else ""
    t = d["AcquisitionTime"] if "AcquisitionTime" in d.columns else pd.Series([""] * len(d), index=d.index)

    def hms(x):
        m = re.match(r"(?:.*T)?(\d+):(\d+):(\d+)", str(x))
        return ":".join(g.zfill(2) for g in m.groups()) if m else str(x)[:8]
    d["time"] = t.map(hms)
    d["row"] = [str(i + 1) for i in range(len(d))]
    return d


# ---------------------------------------------------------------- checks
def checks(d):
    """d = add_calc() frame (edits already applied). Returns (problem lines, info lines)"""
    bad, info = [], []
    lab = d[d["label"] != ""]
    skip = d["SeriesDescription"].str.contains("|".join(SKIP_WORDS))
    dup = d["SeriesDescription"].isin(set(lab["SeriesDescription"]))     # e.g. the vNav repeat of a labelled T1w
    unl = d[(d["label"] == "") & ~skip & ~dup]
    if len(unl):
        bad.append(f"{len(unl)} series have no label and will NOT be converted (marked '?'):")
        bad += [f"      row {r.row:>3}  series {r.SeriesNumber:>5}  {r.SeriesDescription}" for r in unl.itertuples()]
        bad.append("      Fine if you do not need them. To convert one, give it a label.")
    fn = lab[lab["label"].isin(FUNC)]
    if len(fn):
        keys = ["label", "EchoNumber", "PEdir", "acq", "inv", "run"]
        for k, g in fn.groupby(keys):
            have = dict(zip(g["MP"], g["SeriesNumber"]))
            ne = k[1] or "?"
            if {"M", "P"} <= set(have):
                info.append(f"OK  {k[0]} run {k[5]}: magnitude series {have['M']} + phase series {have['P']}   "
                            f"({ne} echoes, PE {k[2] or '?'})")
            else:
                miss = "phase" if "M" in have else "magnitude" if "P" in have else "magnitude and phase"
                bad.append(f"MISSING {miss} for {k[0]} run {k[5]} (series {', '.join(g['SeriesNumber'])}): "
                           f"NORDIC needs both. Check the MP column.")
    else:
        bad.append("NO functional (rest/task) series are labelled - NORDIC (step 03) would have nothing to do.")
    for r in lab[lab["label"].str.lower().str.startswith("fmap") & (lab["PEdir"] == "")].itertuples():
        bad.append(f"fmap series {r.SeriesNumber} has no PEdir (AP/PA) - fill it in.")
    for r in lab[lab["consistent"] == "0"].itertuples():
        bad.append(f"series {r.SeriesNumber} ({r.SeriesDescription}) has files with different dimensions (consistent=0).")
    fdirs = sorted(set(lab[lab["label"].str.lower().str.startswith("fmap")]["PEdir"]) - {""})
    if fdirs:
        info.append(f"fmap directions present: {', '.join(fdirs)}")
    counts = lab.groupby("label").size()
    info.append("will be converted: " + (", ".join(f"{l} x{n}" for l, n in counts.items()) or "nothing is labelled yet"))
    return bad, info


# ---------------------------------------------------------------- view file
def render(tsv, d, bad, info, note=""):
    cols = [(k, h) for k, h in COLS]
    body = {k: [show(str(v)) for v in d[k]] for k, _ in cols if k != "row" or True}
    widths = {k: max(len(h), max((len(x) for x in body[k]), default=0)) for k, h in cols}
    last = cols[-1][0]
    fmt = lambda vals: "  ".join(vals[k].ljust(widths[k]) if k != last else vals[k] for k, _ in cols).rstrip()
    sub = d["subID"].iloc[0] if "subID" in d.columns and len(d) else ""
    ses = d["ses"].iloc[0] if "ses" in d.columns and len(d) else ""
    L = [f"# VIEW of {Path(tsv).name}   (sub {sub} ses {ses}, {len(d)} series rows)   {note}".rstrip(),
         "# Edit only columns marked *  (label, MP, acq, PEdir, inv, nEcho).  Empty = a single dot  .",
         "# '?' at the start of a line = no label, series will NOT be converted.  Do not add/remove/reorder lines.",
         "# After editing:  python helpers/view_tsv.py status <tsv>   (preview + checks)",
         "#                 step 02 with --use-view copies your edits into the TSV; without it, edits here are NOT used.",
         "#",
         "  " + fmt({k: h for k, h in cols}),
         "  " + "  ".join("-" * widths[k] for k, _ in cols)]
    unl = (d["label"] == "") & ~d["SeriesDescription"].str.contains("|".join(SKIP_WORDS)) \
        & ~d["SeriesDescription"].isin(set(d[d["label"] != ""]["SeriesDescription"]))
    for i in range(len(d)):
        mark = "?" if unl.iloc[i] else " "
        L.append(mark + " " + fmt({k: body[k][i] for k, _ in cols}))
    L += ["", "# CHECKS (recalculated whenever you run make/status)"]
    L += ["#   " + x for x in (bad or ["no problems found"])]
    L += ["#"] + ["#   " + x for x in info]
    L += ["#", f"# real file: {Path(tsv).resolve()}"]
    return "\n".join(L) + "\n"


def parse_view(view):
    """-> {row_number: {col: value}}  (editable columns only), list of error strings"""
    out, errs = {}, []
    ncol = len(COLS)
    for n, line in enumerate(Path(view).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        tok = line[1:].split(None, ncol - 1) if line[0] in "? " else line.split(None, ncol - 1)
        if not tok or not tok[0].isdigit():
            continue                                   # header / dashes
        if len(tok) < ncol:
            errs.append(f"line {n}: expected {ncol} columns, found {len(tok)} (empty cells must be written as '.')")
            continue
        rec = dict(zip([k for k, _ in COLS], tok))
        row = int(rec["row"])
        vals = {c: unshow(rec[c]) for c in EDIT}
        if vals["MP"] not in OK_MP:
            errs.append(f"line {n} (row {row}): MP must be M, P or '.', got '{vals['MP']}'")
        if vals["PEdir"] not in OK_PE:
            errs.append(f"line {n} (row {row}): PEdir must be one of AP PA LR RL IS SI or '.', got '{vals['PEdir']}'")
        for c in ("inv", "EchoNumber"):
            if vals[c] and not re.fullmatch(r"\d+(\.0+)?", vals[c]):
                errs.append(f"line {n} (row {row}): {c} must be a whole number or '.', got '{vals[c]}'")
        out[row] = {"vals": vals, "series": rec["SeriesNumber"], "desc": rec["SeriesDescription"]}
    return out, errs


# ---------------------------------------------------------------- commands
def pending(tsv):
    """compare view.txt with the snapshot. -> (changes, base_df, cur_df, errors)
       changes = [(row, col, base_value, new_value)]"""
    tsv, view, basef = paths(tsv)
    if not view.exists() or not basef.exists():
        return None, None, None, []
    base, cur = read_tsv(basef), read_tsv(tsv)
    parsed, errs = parse_view(view)
    if errs:
        return None, base, cur, errs
    if len(base) != len(cur) or len(parsed) != len(base):
        return None, base, cur, [f"the view has {len(parsed)} rows, the snapshot {len(base)}, the TSV {len(cur)}. "
                                 f"The TSV was regenerated or lines were added/removed. Rebuild with:  "
                                 f"python view_tsv.py make {tsv} --force   (this discards the unapplied edits)"]
    ident = lambda df: list(zip(df["SeriesNumber"], df["filename"] if "filename" in df.columns else df["SeriesDescription"]))
    if ident(base) != ident(cur):
        return None, base, cur, [f"{tsv.name} no longer matches the snapshot rows (was step 01 rerun?). "
                                 f"Rebuild with:  python view_tsv.py make {tsv} --force"]
    ch = []
    for row, rec in sorted(parsed.items()):
        for c in EDIT:
            b = norm(c, base.at[row - 1, c])
            if rec["vals"][c] != b:
                ch.append((row, c, b, rec["vals"][c]))
    return ch, base, cur, []


def fmt_changes(ch, cur):
    return [f"   row {r:>3} (series {cur.at[r-1, 'SeriesNumber']}, {cur.at[r-1, 'SeriesDescription']}): "
            f"{c} '{b}' -> '{n}'" for r, c, b, n in ch]


def cmd_make(a):
    tsv, view, basef = paths(a.tsv)
    if view.exists() and basef.exists() and not a.force:
        ch, _, _, errs = pending(tsv)
        if errs or ch:
            sys.exit(f"ERROR: {view.name} has unapplied edits - not overwriting it.\n"
                     f"  apply them:  python view_tsv.py apply {tsv}\n  or discard them:  rerun with --force")
    d = add_calc(read_tsv(tsv))
    bad, info = checks(d)
    shutil.copyfile(tsv, basef)
    text = render(tsv, d, bad, info)
    view.write_text(text)
    if a.print:
        print(text)
    print(f"view written: {view}")


def cmd_status(a):
    tsv, view, basef = paths(a.tsv)
    ch, base, cur, errs = pending(tsv)
    if ch is None and not errs:
        print(f"no view file for {tsv.name} - the TSV alone will be used."); return 0
    if errs:
        print("PROBLEMS in the view file:"); [print("  " + e) for e in errs]; return 2
    if norm_df(cur) != norm_df(base):
        print("note: the TSV itself was edited after the view was made (e.g. in LibreOffice). Those edits are kept.")
    if not ch:
        print(f"no edits waiting in {view.name}."); return 0
    print(f"{len(ch)} edit(s) waiting in {view.name} (not applied yet):"); [print(x) for x in fmt_changes(ch, cur)]
    prev = cur.copy()
    for r, c, b, n in ch:
        prev.at[r - 1, c] = n
    bad, info = checks(add_calc(prev))
    print("\nCHECKS if these edits are applied:")
    [print("  " + x) for x in (bad or ["no problems found"])]; [print("  " + x) for x in info]
    return 10


def norm_df(df):
    return [[norm(c, v) for c, v in zip(df.columns, row)] for row in df.itertuples(index=False)]


def cmd_apply(a):
    tsv, view, basef = paths(a.tsv)
    ch, base, cur, errs = pending(tsv)
    if ch is None and not errs:
        print(f"no view file for {tsv.name} - using the TSV as it is."); return 0
    if errs:
        print("ERROR in the view file - nothing was changed:"); [print("  " + e) for e in errs]; return 2
    if not ch:
        print("no edits in the view file - using the TSV as it is."); return 0
    conflicts, todo = [], []
    for r, c, b, n in ch:
        now = norm(c, cur.at[r - 1, c])
        if now == n:
            continue
        if now == b:
            todo.append((r, c, b, n))
        else:
            conflicts.append(f"   row {r} (series {cur.at[r-1, 'SeriesNumber']}) {c}: the TSV now has '{now}', "
                             f"the view was changed from '{b}' to '{n}'")
    if conflicts:
        print("CONFLICT - the same cell was changed differently in the TSV and in the view. Nothing was written:")
        [print(x) for x in conflicts]
        print(f"Fix one of them (edit the view, or the TSV) and run again.  Or discard the view: "
              f"python view_tsv.py make {tsv} --force")
        return 2
    if todo:
        bak = tsv.with_name(tsv.name + time.strftime(".bak-%Y%m%d-%H%M%S"))
        shutil.copyfile(tsv, bak)
        for r, c, b, n in todo:
            cur.at[r - 1, c] = n
        cur.to_csv(tsv, sep="\t", index=False)
        print(f"applied {len(todo)} edit(s) from {view.name} to {tsv.name}  (backup: {bak.name})")
        [print(x) for x in fmt_changes(todo, cur)]
    else:
        print("the TSV already contains every edit in the view.")
    a.force = True; a.print = False
    cmd_make(a)                                       # view + snapshot now match the TSV again
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("make", "status", "apply"):
        s = sp.add_parser(name); s.add_argument("tsv")
        if name == "make":
            s.add_argument("--force", action="store_true"); s.add_argument("--print", action="store_true")
    a = ap.parse_args()
    if a.cmd == "make":
        cmd_make(a)
    else:
        sys.exit({"status": cmd_status, "apply": cmd_apply}[a.cmd](a))
