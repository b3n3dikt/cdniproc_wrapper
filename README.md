# cdniproc_wrapper

SLURM wrapper around the CDNI lab's `cdniproc_v2.0` tools. It takes a session from raw DICOMs on S3 to cleaned fMRI data:

```
S3 DICOMs → 01 summary TSV → 02 dcm2bids → 03 NORDIC → 04 clean + layout + IntendedFor → 05 fMRIPrep → XCP-D
```

It does not reimplement the lab code. It calls the shared versions on the MSI server (`cdniproc_v2.0`, `Dcm2bids3_NORDIC_wrapper`, the fMRIPrep / XCP-D containers) and handles the glue: paths, job chaining, waiting for NORDIC, building the final BIDS datasets. The full write-up is in [`docs/SOP_cdniproc_wrapper.md`](docs/SOP_cdniproc_wrapper.md) (also as [Word](docs/SOP_cdniproc_wrapper.docx)).

## Quick start

```bash
git clone https://github.com/b3n3dikt/cdniproc_wrapper.git && cd cdniproc_wrapper
$EDITOR config.sh      # set your SLURM account, S3 bucket, output location (see below)
mkdir -p logs

# Example: subject SUB001, scanner 3T, two sessions 01 and 02.
#   SUB001 = subject ID | 3T = MAGNET (field strength) | 01 02 = session labels (ses-01 and ses-02)

# Step 1 only (--to 1): pull the DICOMs and make one summary TSV per session, so you can review them
./run_subject.sh SUB001 3T 01 02 --to 1

#   ...open $OUT_BASE/3T/summaries/sub-SUB001_ses-01.tsv (or its .view.txt), check/fix the label, MP columns...

# Steps 2-5 (--from 2): convert to BIDS, NORDIC, build the datasets, fMRIPrep + XCP-D, chained with --dependency=afterok
./run_subject.sh SUB001 3T 01 02 --from 2

# MAGNET is optional: left out, DEFAULT_MAGNET from config.sh (3T) is used
./run_subject.sh SUB001 01 02 --from 2
```

- `SUB` = subject ID without `sub-`.
- `MAGNET` = `3T` or `7T`. **Optional.** Only studies with both field strengths need to type it. Everyone else leaves it out and, if their study is not 3T, sets `DEFAULT_MAGNET="7T"` once in `config.sh`.
- `SES` = one or more session labels without `ses-`, as they appear in your S3 folder names (see `S3_DICOM_PATH` in `config.sh`). Numeric sessions should be two digits (`01`); labels like `3TD1` or `7T1` are fine as they are.
- `--from N` / `--to N` pick steps 1–5. Always run from this folder.

## The one file you edit: `config.sh`

| Section | What you set |
|---|---|
| 1. who / where | `SLURM_ACCOUNT`, `S3_BUCKET`, `S3_DICOM_PATH` (where a session's DICOMs are in the bucket, e.g. `pfm3t7t/{MAGNET}/dicoms/sub-{SUB}_ses-{SES}/`), `DEFAULT_MAGNET`, optional `STUDY_NAME`, `OUT_BASE` (scratch output location; must not contain `bids`, `sub-`, `ses-`) |
| 2. shared lab code | Paths to `cdniproc_v2.0` etc. Normally unchanged. `NORDIC_SBATCH` only if you need a modified NORDIC script |
| 3. software | `CONDA_ENV` (needs dcm2bids v3, dcm2niix, pandas, nibabel), module names |
| 4. pipeline versions | `FMRIPREP_VERSION`, `XCPD_VERSION`, `CIFTI_SPACE` |
| 5. SLURM resources | `RES_0N_CPUS / MEM / TIME / PART` for each step script, and `RES_NORDIC_*` for the NORDIC jobs step 03 submits (partition, time, memory) |
| 6. fMRIPrep + XCP-D flags | `FMRIPREP_FLAGS` and `XCPD_FLAGS` arrays (defaults = the lab's abcd-mode settings) |
| 7. choices | `INTENDEDFOR_METHOD` (`jsbr` or `lab`), `SESSIONS_ANAT` |

`run_subject.sh` applies the account and the resources from `config.sh` when it submits each job. A bare `sbatch 0X_*.sh` uses the defaults written in that script's `#SBATCH` header (and your default SLURM account) instead, so use `run_subject.sh --from N --to N` to run a single step.

Also per study: `helpers/label_rules.csv` (series names the automatic labeller misses) and `helpers/dataset_description.json` (study name, authors).

## Steps

| # | Script | Per | What it does | Lab code it calls |
|---|---|---|---|---|
| 01 | `01_pull_and_make_tsv.sh` | session | Last DICOM of each series from S3 → dcm2niix → summary TSV you review | `tools/convert_helper.py`, `nii_init_gpt5.py` |
| 02 | `02_convert_to_bids.sh` | session | TSV → dcm2bids config, full DICOM sync, dcm2bids | `tsv_to_json.py`, `dcm2bids` |
| 03 | `03_run_nordic.sh` | session | Writes and submits NORDIC jobs (mag+phase → denoised). 7T: MP2RAGE denoise + bias-field jobs | `tools/make_nordic_cmds.py`, `nordicsbatch_new.sh`, `runnordic.m` |
| 04 | `04_clean_and_layout.sh` | subject | Waits for NORDIC, checks it, builds `bids_combined/` and `bids_sessions/`, writes IntendedFor | `tools/postnordic.py`, optionally `tools/IntendedFor_new.py` |
| 05 | `05_fmriprep_xcpd.sh` | subject | fMRIPrep, XCP-D, motion/interpolation conversions, template-matching input | containers, `tools/copy_files_to_temp.py`, `xcpd2dcanmotion` |

Our own code is in `helpers/` (`apply_label_rules.py`, `make_layouts.py`, `IntendedFor_JSBR.py`, the 7T anatomical scripts).

### Reviewing the TSV (step 01 → 02)
**Two ways to review and fix the TSV.** Both end with the same TSV, which is what `tsv_to_json.py` reads in step 02.

1. *The original way:* open `summaries/sub-X_ses-Y.tsv` in LibreOffice (or any editor), fix it, save, run step 02. Nothing else to do.
2. *The text-file way (no LibreOffice):* step 01 also writes `summaries/sub-X_ses-Y.view.txt`, a short aligned table with the columns that matter, plus a CHECKS section (unlabelled series, rest runs missing a magnitude/phase partner, fmaps without PEdir). Open it in VS Code or `nano` and edit the columns marked `*` (`label, MP, acq, PEdir, inv, nEcho`; an empty cell is a single `.`). Then:
   ```bash
   python helpers/view_tsv.py status summaries/sub-X_ses-Y.tsv     # preview: lists your edits + re-runs the checks
   ./run_subject.sh SUB001 3T 01 02 --from 2 --use-view                 # step 02 copies your edits into the TSV first
   ```

How the two are kept from fighting each other:
- The TSV is always the source of truth. `--use-view` never regenerates it from the text file; it only writes the **cells you changed** in the text file (compared with a hidden snapshot taken when the view was made). Anything changed in the TSV by hand (LibreOffice) is kept.
- Same cell changed to different values in both files: nothing is written and the conflict is listed.
- Without `--use-view`, step 02 uses the TSV exactly as before. If the text file contains edits that are not in the TSV, step 02 **stops** and tells you, so edits are never silently ignored.
- A timestamped backup of the TSV (`*.tsv.bak-…`) is made before any edit is written, and the view is refreshed after step 02 (`tsv_to_json.py` recalculates `runNum`).
- Rerunning step 01 rebuilds the TSV and the view; the previous ones are kept as `*.prev`.
- `python helpers/view_tsv.py make <tsv> --force` throws away the text edits and rebuilds the view from the TSV.

One row per DICOM series. Check `label` (`rest`, `fmap`, `t1w`, ... — **empty means not converted**), `MP` (`M` magnitude / `P` phase; NORDIC needs both for each rest run), `PEdir` (AP/PA for fmaps), `EchoNumber`. Edit the cells directly.

### Where everything goes (`$OUT_BASE/<MAGNET>/`)
| Folder | Made by | What |
|---|---|---|
| `dicoms_last/`, `helper/` | 01 | 1 DICOM per series and its quick conversion (only used for the TSV) |
| `summaries/` | 01–04 | `sub-X_ses-Y.tsv/.json`, `nordic_cmd_*.sh`, `layout_map_sub-X.tsv` |
| `dicoms/` | 02 | full DICOMs |
| `bids/` | 02, 03 | raw BIDS (mag + phase + NORDIC output next to them) |
| `derivatives/nordic/` | 04 | NORDIC-cleaned sessions |
| `bids_combined/` | 04 | fMRIPrep input: `sub-X/ses-combined/{anat,func,fmap}` |
| `bids_sessions/` | 04 | `sub-X/anat` + `sub-X/ses-N/{func,fmap}` |
| `derivatives/91k_fmriprep_*`, `91k_xcpd_*` | 05 | pipeline outputs; XCP-D is the deliverable |
| `jobs/`, `logs/` | all | child job IDs, NORDIC and postnordic logs |

### 3T vs 7T
Only needed if your study has both. The `MAGNET` argument selects it (and names the output sub-folder `3T/` or `7T/`). 7T additionally denoises the MP2RAGE and starts bias-field correction in step 03 (needs `helpers/Bias_field_script_job.m`, not included here), and step 04 copies in the final T1w/T2w and fabricates the AP fieldmap (7T fmaps are PA only).

## Troubleshooting
| Symptom | Fix |
|---|---|
| Job ran but no log files | The log folder did not exist. `mkdir -p logs`. `run_subject.sh` passes absolute log paths so this only bites bare `sbatch` runs from another directory |
| Job fails immediately | Submitted from a different directory so `config.sh` was not found. `cd` here first |
| `OUT_BASE contains bids/sub-/ses-` | Pick another `OUT_BASE` (the lab NORDIC scripts split paths on those words) |
| "no `*_part-mag_*` func files" (02) | TSV has no `M`/`P` rows for the rest runs; fix and rerun 02 |
| `Invalid account or account/partition` in step 03 | The lab NORDIC script hard-codes `-p msismall`. Set `RES_NORDIC_PART` in `config.sh` to a partition your account can use, rerun 03 |
| NORDIC `TIMEOUT` | Raise `RES_NORDIC_TIME` in `config.sh` (lab default 1.5 h), rerun 03 then 04 |
| `NORDIC FAILED` (04) | Read `$OUT_BASE/<MAGNET>/logs/nordic/*.err`, rerun 03 for that session, rerun 04 |
| BIDS validator/fMRIPrep rejects `bids_sessions` | Set `SESSIONS_ANAT="session"` and rerun 04 (`bids_combined` is unaffected) |

## Notes
- The `*_dev.py` scripts and `archive/` in `cdniproc_v2.0` are older/experimental and are not used. The lab's `combineSessions_gpt3.py` is not used either: it would skip the NORDIC functional files.
- Not covered yet: PCM / template matching (the lab's `submit_PCM.sh`, `submit_TM.sh`).
- `config.sh` ships with placeholders (`YOUR_SLURM_ACCOUNT`, `YOUR_S3_BUCKET`). `run_subject.sh` refuses to run until you replace them. `STUDY_NAME` is optional and only names a folder inside `OUT_BASE`.
