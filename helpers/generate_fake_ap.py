#!/usr/bin/env python3
import os
import re
import json
import subprocess
import sys
import argparse
import shutil
import textwrap
from glob import glob

# -------------------------------------------------------------------------
# helper utilities
# -------------------------------------------------------------------------
def msg(txt):
    """Print a wrapped info line."""
    print(textwrap.fill(txt, subsequent_indent="      "))

def ensure_prefix(val: str, prefix: str) -> str:
    """Ensure val starts with prefix (idempotent)."""
    if val.startswith(prefix):
        return val
    return f"{prefix}{val}"

def strip_prefix(val: str, prefix: str) -> str:
    """Remove prefix from start if present."""
    return val[len(prefix):] if val.startswith(prefix) else val

def get_first_intended_for(json_file):
    """Return first entry in IntendedFor list; None if absent/empty."""
    with open(json_file, "r") as f:
        data = json.load(f)
    lst = data.get("IntendedFor", [])
    return lst[0] if lst else None

def resolve_func_path(bids_root, subj_label, intended_path):
    """
    Build an absolute path to the functional NIfTI from an IntendedFor entry.
    Accepts either:
      - 'ses-XXX/func/...' (preferred)
      - 'sub-YYY/ses-XXX/func/...' (older style)
      - absolute path (returned as-is)
    """
    if os.path.isabs(intended_path):
        return intended_path

    # If starts with sub-..., treat as relative to BIDS root
    if intended_path.split("/", 1)[0].startswith("sub-"):
        return os.path.join(bids_root, intended_path)

    # Otherwise assume relative to sub-<ID>
    return os.path.join(bids_root, subj_label, intended_path)

def extract_run_numbers(json_names):
    """
    Robustly extract run numbers from a list of fmap JSON basenames.
    Returns a set of strings like {'01','02',...}.
    """
    runs = set()
    for name in json_names:
        m = re.search(r"_run-(\d+)_", name)
        if m:
            runs.add(m.group(1))
    return runs

def create_fake_ap(bids_root, func_rel_or_abs, subj, ses, run, verbose=False):
    """
    Create an AP EPI from the first 30 volumes of an existing functional run.

    Parameters
    ----------
    bids_root : path to BIDS root (e.g. .../7T/bids)
    func_rel_or_abs : IntendedFor value or absolute path to the functional NIfTI
    subj, ses, run : identifiers (without 'sub-'/'ses-' prefixes)
    """
    subj_label = ensure_prefix(subj, "sub-")
    ses_label  = ensure_prefix(ses,  "ses-")

    func_abs = resolve_func_path(bids_root, subj_label, func_rel_or_abs)
    fmap_dir = os.path.join(bids_root, subj_label, ses_label, "fmap")
    os.makedirs(fmap_dir, exist_ok=True)

    out_epi = os.path.join(fmap_dir, f"{subj_label}_{ses_label}_dir-AP_run-{run}_epi.nii.gz")
    out_json = out_epi.replace(".nii.gz", ".json")

    if verbose:
        msg(f"fslroi:  {func_abs}  ->  {out_epi}")

    fslroi = shutil.which("fslroi")
    if not fslroi:
        sys.exit("ERROR: fslroi not found in PATH (did you load the FSL module?)")

    if not os.path.exists(func_abs):
        msg(f"WARNING: functional file does not exist:\n  {func_abs}\n  Skipping fake AP for run {run}.")
        return

    # grab 30 volumes (timepoints 10-30) from the original run
    subprocess.run(
        [fslroi, func_abs, out_epi, "0", "-1", "0", "-1", "0", "-1", "10", "30"],
        check=True,
    )

    # copy JSON sidecar from the functional file
    func_json = func_abs.replace(".nii.gz", ".json")
    if os.path.exists(func_json):
        shutil.copy2(func_json, out_json)
    else:
        msg(f"WARNING: missing func JSON to copy into AP sidecar:\n  {func_json}")

def process_one_session(bids_root, subj, ses, verbose=False):
    subj_label = ensure_prefix(subj, "sub-")
    ses_label  = ensure_prefix(ses,  "ses-")
    fmap_dir = os.path.join(bids_root, subj_label, ses_label, "fmap")

    if not os.path.isdir(fmap_dir):
        msg(f"INFO: no fmap directory at {fmap_dir}  nothing to do.")
        return

    basenames = [f for f in os.listdir(fmap_dir) if f.endswith(".json")]
    pa_jsons = [f for f in basenames if "dir-PA" in f]
    ap_jsons = [f for f in basenames if "dir-AP" in f]

    pa_runs = extract_run_numbers(pa_jsons)
    ap_runs = extract_run_numbers(ap_jsons)
    missing_runs = sorted(pa_runs - ap_runs)

    if verbose:
        msg(f"PA runs present: {sorted(pa_runs)}")
        msg(f"AP runs present: {sorted(ap_runs)}")
        if missing_runs:
            msg(f"Need to fake AP for runs: {missing_runs}")
        else:
            msg("All runs already have AP  nothing to add.")

    for run in missing_runs:
        pa_json = os.path.join(fmap_dir, f"{subj_label}_{ses_label}_dir-PA_run-{run}_epi.json")
        func_rel = get_first_intended_for(pa_json)
        if not func_rel:
            msg(f"WARNING: IntendedFor missing in {pa_json}  skipping run {run}")
            continue
        create_fake_ap(bids_root, func_rel, subj, ses, run, verbose)

def find_sessions(bids_root, subj):
    """Return a sorted list like ['ses-7T1','ses-7T2',...] under sub-<ID>."""
    subj_label = ensure_prefix(subj, "sub-")
    subj_dir = os.path.join(bids_root, subj_label)
    if not os.path.isdir(subj_dir):
        return []
    ses_dirs = sorted([os.path.basename(p) for p in glob(os.path.join(subj_dir, "ses-*")) if os.path.isdir(p)])
    return ses_dirs

# -------------------------------------------------------------------------
# command-line interface
# -------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Create fake dir-AP EPI files whenever only dir-PA exists."
    )
    parser.add_argument("work_dir", help="BIDS root directory (e.g. /path/7T/bids)")
    parser.add_argument("SUBID", help="Subject ID (e.g. PFM3T7T01 or sub-PFM3T7T01)")
    parser.add_argument("SES", nargs="?", help="Optional session (e.g. 7T2 or ses-7T2). If omitted, run all sessions.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    bids_root = args.work_dir
    subj = args.SUBID

    if args.SES:
        # Run a single session
        ses = args.SES
        process_one_session(bids_root, subj, ses, verbose=args.verbose)
    else:
        # Run all sessions under the subject
        ses_list = find_sessions(bids_root, subj)
        if not ses_list:
            msg(f"INFO: No sessions found under {ensure_prefix(subj, 'sub-')} in {bids_root}. Nothing to do.")
            return
        for ses_label in ses_list:
            ses = strip_prefix(ses_label, "ses-")
            msg(f"=== Processing {ensure_prefix(subj,'sub-')} {ses_label} ===")
            process_one_session(bids_root, subj, ses, verbose=args.verbose)

if __name__ == "__main__":
    main()

# #!/usr/bin/env python3
# import os
# import json
# import subprocess
# import sys
# import argparse
# import shutil
# import textwrap

# # -------------------------------------------------------------------------
# # helper utilities
# # -------------------------------------------------------------------------
# def msg(txt):
#     """Print a wrapped info line."""
#     print(textwrap.fill(txt, subsequent_indent="      "))


# def get_first_intended_for(json_file):
#     """Return first entry in IntendedFor list; None if absent/empty."""
#     with open(json_file, "r") as f:
#         data = json.load(f)
#     lst = data.get("IntendedFor", [])
#     return lst[0] if lst else None


# def create_fake_ap(bids_root, func_rel_path, subj, ses, run, verbose=False):
#     """
#     Create an AP EPI from the first 30 volumes of an existing functional run.

#     Parameters
#     ----------
#     bids_root : path to BIDS root (e.g. .../7T/bids)
#     func_rel_path : path *inside* sub-<ID>/ directory, e.g.
#                     ses-7T1/func/sub-XXX_ses-7T1_task-rest_bold.nii.gz
#     subj, ses, run : identifiers
#     """
#     func_abs = os.path.join(bids_root, f"sub-{subj}", func_rel_path)
#     fmap_dir = os.path.join(bids_root, f"sub-{subj}", f"ses-{ses}", "fmap")
#     os.makedirs(fmap_dir, exist_ok=True)

#     out_epi = os.path.join(
#         fmap_dir, f"sub-{subj}_ses-{ses}_dir-AP_run-{run}_epi.nii.gz"
#     )
#     out_json = out_epi.replace(".nii.gz", ".json")

#     if verbose:
#         msg(f"fslroi:  {func_abs}  ->  {out_epi}")

#     fslroi = shutil.which("fslroi")
#     if not fslroi:
#         sys.exit("ERROR: fslroi not found in PATH (did you load the FSL module?)")

#     # grab 30 volumes (timepoints 10-30) from the original run
#     subprocess.run(
#         [fslroi, func_abs, out_epi, "0", "-1", "0", "-1", "0", "-1", "10", "30"],
#         check=True,
#     )

#     # copy JSON sidecar
#     shutil.copy2(func_abs.replace(".nii.gz", ".json"), out_json)


# def main(bids_root, subj, ses, verbose=False):
#     fmap_dir = os.path.join(bids_root, f"sub-{subj}", f"ses-{ses}", "fmap")
#     if not os.path.isdir(fmap_dir):
#         msg(f"INFO: no fmap directory at {fmap_dir}  nothing to do.")
#         return

#     pa_jsons = [
#         f for f in os.listdir(fmap_dir) if f.endswith(".json") and "dir-PA" in f
#     ]
#     ap_jsons = [
#         f for f in os.listdir(fmap_dir) if f.endswith(".json") and "dir-AP" in f
#     ]

#     pa_runs = {f.split("_")[3].split("-")[1] for f in pa_jsons}
#     ap_runs = {f.split("_")[3].split("-")[1] for f in ap_jsons}
#     missing_runs = sorted(pa_runs - ap_runs)

#     if verbose:
#         msg(f"PA runs present: {sorted(pa_runs)}")
#         msg(f"AP runs present: {sorted(ap_runs)}")
#         if missing_runs:
#             msg(f"Need to fake AP for runs: {missing_runs}")
#         else:
#             msg("All runs already have AP  nothing to add.")

#     for run in missing_runs:
#         pa_json = os.path.join(
#             fmap_dir, f"sub-{subj}_ses-{ses}_dir-PA_run-{run}_epi.json"
#         )
#         func_rel = get_first_intended_for(pa_json)
#         if not func_rel:
#             msg(f"WARNING: IntendedFor missing in {pa_json}  skipping run {run}")
#             continue
#         create_fake_ap(bids_root, func_rel, subj, ses, run, verbose)


# # -------------------------------------------------------------------------
# # command-line interface
# # -------------------------------------------------------------------------
# if __name__ == "__main__":
#     parser = argparse.ArgumentParser(
#         description="Create fake dir-AP EPI files whenever only dir-PA exists."
#     )
#     parser.add_argument("work_dir", help="BIDS root directory (e.g. /path/7T/bids)")
#     parser.add_argument("SUBID")
#     parser.add_argument("SES")
#     parser.add_argument("-v", "--verbose", action="store_true")
#     args = parser.parse_args()

#     main(args.work_dir, args.SUBID, args.SES, verbose=args.verbose)


# import os
# import json
# import subprocess
# import sys


# def get_first_intended_for(json_file):
#     with open(json_file, 'r') as f:
#         data = json.load(f)
#     intended_for = data.get("IntendedFor", [])
#     if intended_for:
#         return intended_for[0]
#     return None

# def create_fake_ap(work_dir, func_file, subj_id, ses_id, run_id):
#     func_file_path = os.path.join(work_dir, f"sub-{subj_id}", func_file)
#     fmap_dir = os.path.join(work_dir, f"sub-{subj_id}/ses-{ses_id}/fmap")
#     output_file = os.path.join(fmap_dir, f"sub-{subj_id}_ses-{ses_id}_dir-AP_run-{run_id}_epi.nii.gz")
    
#     # Run the fslroi command
#     fslroi_cmd = [
#         "fslroi", func_file_path, output_file, "0", "-1", "0", "-1", "0", "-1", "10", "30"
#     ]
#     subprocess.run(fslroi_cmd)

#     # Copy the json file
#     func_json_file = func_file_path.replace(".nii.gz", ".json")
#     output_json_file = output_file.replace(".nii.gz", ".json")
#     subprocess.run(["cp", func_json_file, output_json_file])

# def main(work_dir, subj_id, ses_id):
#     fmap_dir = os.path.join(work_dir, f"sub-{subj_id}/ses-{ses_id}/fmap")
#     func_dir = os.path.join(work_dir, f"sub-{subj_id}/ses-{ses_id}/func")

#     pa_files = [f for f in os.listdir(fmap_dir) if "dir-PA" in f and f.endswith(".json")]
#     ap_files = [f for f in os.listdir(fmap_dir) if "dir-AP" in f and f.endswith(".json")]

#     ap_runs = set(f.split("_")[3].split("-")[1] for f in ap_files)
#     pa_runs = set(f.split("_")[3].split("-")[1] for f in pa_files)

#     missing_ap_runs = pa_runs - ap_runs

#     for run_id in missing_ap_runs:
#         pa_json_file = os.path.join(fmap_dir, f"sub-{subj_id}_ses-{ses_id}_dir-PA_run-{run_id}_epi.json")
#         func_file = get_first_intended_for(pa_json_file)
        
#         if func_file:
#             create_fake_ap(work_dir, func_file, subj_id, ses_id, run_id)

# if __name__ == "__main__":
#     if len(sys.argv) != 4:
#         print("Usage: python generate_fake_ap.py <work_dir> <SUBID> <SES>")
#         sys.exit(1)
    
#     work_dir = sys.argv[1]
#     subj_id = sys.argv[2]
#     ses_id = sys.argv[3]
    
#     main(work_dir, subj_id, ses_id)




# import os
# import json
# import subprocess
# import sys

# def get_first_intended_for(json_file):
#     with open(json_file, 'r') as f:
#         data = json.load(f)
#     intended_for = data.get("IntendedFor", [])
#     if intended_for:
#         return intended_for[0]
#     return None

# def create_fake_ap(work_dir, func_file, subj_id, ses_id, run_id):
#     func_file_path = os.path.join(work_dir, func_file)
#     fmap_dir = os.path.join(work_dir, f"bids/sub-{subj_id}/ses-{ses_id}/fmap")
#     output_file = os.path.join(fmap_dir, f"sub-{subj_id}_ses-{ses_id}_dir-AP_run-{run_id}_epi.nii.gz")
    
#     # Run the fslroi command
#     fslroi_cmd = [
#         "fslroi", func_file_path, output_file, "0", "-1", "0", "-1", "0", "-1", "10", "30"
#     ]
#     subprocess.run(fslroi_cmd)

#     # Copy the json file
#     func_json_file = func_file_path.replace(".nii.gz", ".json")
#     output_json_file = output_file.replace(".nii.gz", ".json")
#     subprocess.run(["cp", func_json_file, output_json_file])

# def main(work_dir, subj_id, ses_id):
#     fmap_dir = os.path.join(work_dir, f"bids/sub-{subj_id}/ses-{ses_id}/fmap")
#     func_dir = os.path.join(work_dir, f"bids/sub-{subj_id}/ses-{ses_id}/func")

#     pa_files = [f for f in os.listdir(fmap_dir) if "dir-PA" in f and f.endswith(".json")]
#     ap_files = [f for f in os.listdir(fmap_dir) if "dir-AP" in f and f.endswith(".json")]

#     ap_runs = set(f.split("_")[3].split("-")[1] for f in ap_files)
#     pa_runs = set(f.split("_")[3].split("-")[1] for f in pa_files)

#     missing_ap_runs = pa_runs - ap_runs

#     for run_id in missing_ap_runs:
#         pa_json_file = os.path.join(fmap_dir, f"sub-{subj_id}_ses-{ses_id}_dir-PA_run-{run_id}_epi.json")
#         func_file = get_first_intended_for(pa_json_file)
        
#         if func_file:
#             create_fake_ap(work_dir, func_file, subj_id, ses_id, run_id)

# if __name__ == "__main__":
#     if len(sys.argv) != 4:
#         print("Usage: python generate_fake_ap.py <work_dir> <SUBID> <SES>")
#         sys.exit(1)
    
#     work_dir = sys.argv[1]
#     subj_id = sys.argv[2]
#     ses_id = sys.argv[3]
    
#     main(work_dir, subj_id, ses_id)



# import os
# import json
# import subprocess
# import sys

# def get_first_intended_for(json_file):
#     with open(json_file, 'r') as f:
#         data = json.load(f)
#     intended_for = data.get("IntendedFor", [])
#     if intended_for:
#         return intended_for[0]
#     return None

# def create_fake_ap(input_dir, func_file, output_dir, subj_id, ses_id, run_id):
#     func_file_path = os.path.join(input_dir, func_file)
#     output_file = os.path.join(output_dir, f"sub-{subj_id}_ses-{ses_id}_dir-AP_run-{run_id}_epi.nii.gz")
    
#     # Run the fslroi command
#     fslroi_cmd = [
#         "fslroi", func_file_path, output_file, "0", "-1", "0", "-1", "0", "-1", "10", "30"
#     ]
#     subprocess.run(fslroi_cmd)

#     # Copy the json file
#     func_json_file = func_file_path.replace(".nii.gz", ".json")
#     output_json_file = output_file.replace(".nii.gz", ".json")
#     subprocess.run(["cp", func_json_file, output_json_file])

# def main(work_dir, subj_id, ses_id):
#     fmap_dir = os.path.join(work_dir, f"bids/sub-{subj_id}/ses-{ses_id}/fmap")
#     func_dir = os.path.join(work_dir, f"bids/sub-{subj_id}/ses-{ses_id}/func")

#     pa_files = [f for f in os.listdir(fmap_dir) if "dir-PA" in f and f.endswith(".json")]
#     ap_files = [f for f in os.listdir(fmap_dir) if "dir-AP" in f and f.endswith(".json")]

#     ap_runs = set(f.split("_")[3].split("-")[1] for f in ap_files)
#     pa_runs = set(f.split("_")[3].split("-")[1] for f in pa_files)

#     missing_ap_runs = pa_runs - ap_runs

#     for run_id in missing_ap_runs:
#         pa_json_file = os.path.join(fmap_dir, f"sub-{subj_id}_ses-{ses_id}_dir-PA_run-{run_id}_epi.json")
#         func_file = get_first_intended_for(pa_json_file)
        
#         if func_file:
#             create_fake_ap(func_dir, func_file, fmap_dir, subj_id, ses_id, run_id)

# if __name__ == "__main__":
#     if len(sys.argv) != 4:
#         print("Usage: python generate_fake_ap.py <work_dir> <SUBID> <SES>")
#         sys.exit(1)
    
#     work_dir = sys.argv[1]
#     subj_id = sys.argv[2]
#     ses_id = sys.argv[3]
    
#     main(work_dir, subj_id, ses_id)