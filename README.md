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

#   ...open $OUT_BASE/summaries/sub-SUB001_ses-01.tsv (or its .view.txt), check/fix the label, MP columns...

# Steps 2-5 (--from 2): convert to BIDS, NORDIC, build the datasets, fMRIPrep + XCP-D, chained with --dependency=afterok
./run_subject.sh SUB001 3T 01 02 --from 2

# MAGNET is optional: left out, DEFAULT_MAGNET from config.sh (3T) is used
./run_subject.sh SUB001 01 02 --from 2
```

- `SUB` = subject ID without `sub-`.
- `MAGNET` = `3T` or `7T`. **Optional.** Only studies with both field strengths need to type it. Everyone else leaves it out and, if their study is not 3T, sets `DEFAULT_MAGNET="7T"` once in `config.sh`.
- `SES` = one or more session labels without `ses-`, as they appear in your S3 folder names (see `S3_DICOM_PATH` in `config.sh`). Numeric sessions should be two digits (`01`); labels like `3T1` or `7T1` are fine as they are.
- `--from N` / `--to N` pick steps 1–5. Always run from this folder.

## The one file you edit: `config.sh`

| Section | What you set |
|---|---|
| 1. who / where | `SLURM_ACCOUNT`, `S3_BUCKET`, `S3_DICOM_PATH` (where a session's DICOMs are in the bucket, e.g. `mystudy/{MAGNET}/dicoms/sub-{SUB}_ses-{SES}/`), `DEFAULT_MAGNET`, optional `STUDY_NAME`, `OUT_BASE` (output location; must not contain `bids`, `sub-`, `ses-`), `USE_MAGNET_FOLDER` (`0` = outputs straight in `OUT_BASE` (default), `1` = in `OUT_BASE/<MAGNET>/`, for studies with both 3T and 7T) |
| 2. shared lab code | Paths to `cdniproc_v2.0` etc. Normally unchanged. `NORDIC_SBATCH` only if you need a modified NORDIC script |
| 3. software | `CONDA_ENV` (needs dcm2bids v3, dcm2niix, pandas, nibabel), module names |
| 4. pipeline versions | `FMRIPREP_VERSION`, `XCPD_VERSION`, `CIFTI_SPACE` |
| 5. SLURM resources | `RES_0N_CPUS / MEM / TIME / PART` for each step script, and `RES_NORDIC_*` for the NORDIC jobs step 03 submits (partition, time, memory) |
| 5b. where step 05 writes | optional `DERIV_BASE` (fMRIPrep/XCP-D results) and `WORK_BASE` (their huge work folders). Empty = `<outputs>/derivatives`. Use `WORK_BASE` to keep the data on `/projects` and the work folders on scratch |
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
| 04 | `04_clean_and_layout.sh` | subject | Waits for NORDIC, checks it, builds `bids_combined/` and `bids_sessions/` (**one session: skipped**, fMRIPrep reads `derivatives/nordic` directly), writes IntendedFor | `tools/postnordic.py`, optionally `tools/IntendedFor_new.py` |
| 05 | `05_fmriprep_xcpd.sh` | subject | fMRIPrep, XCP-D, motion/interpolation conversions, template-matching input | containers, `tools/copy_files_to_temp.py`, `xcpd2dcanmotion` |

Our own code is in `helpers/` (`apply_label_rules.py`, `make_layouts.py`, `IntendedFor_JSBR.py`, the 7T anatomical scripts).

### Reviewing the TSV (step 01 → 02)
Step 01 writes one summary TSV per session, one row per DICOM series. Check:
- `label`: `rest`, `fmap`, `t1w`, ... **Empty means not converted.** (Localizers, scouts and SBRef series are normally left empty.)
- `MP`: `M` magnitude / `P` phase. NORDIC needs both for each rest run.
- `PEdir`: `AP`/`PA` for fmaps. `EchoNumber`: the number of echoes in that series.
- Series names your protocol uses that the labeller misses: add a line to `helpers/label_rules.csv` so it is automatic next time.

**Two ways to review and fix the TSV.** Both end with the same TSV, which is what `tsv_to_json.py` reads in step 02.

1. *The original way:* open `summaries/sub-X_ses-Y.tsv` in LibreOffice (or any editor), fix it, save, run step 02. Nothing else to do.
2. *The text-file way (no LibreOffice):* step 01 also writes `summaries/sub-X_ses-Y.view.txt`, a short aligned table with the columns that matter, plus a CHECKS section (unlabelled series, rest runs missing a magnitude/phase partner, fmaps without PEdir). Open it in VS Code or `nano` and edit the columns marked `*` (`label, MP, acq, PEdir, inv, nEcho`; an empty cell is a single `.`). Then:
   ```bash
   conda activate py11                                              # view_tsv.py needs pandas (same env as the pipeline)
   python helpers/view_tsv.py status summaries/sub-X_ses-Y.tsv      # optional preview: lists your edits + re-runs the checks
   ./run_subject.sh SUB001 3T 01 02 --from 2 --use-txt              # step 02 copies your edits into the TSV first
   ```
   `status` is optional: `--use-txt` applies the edits by itself. Running `status` first just catches a typo in seconds instead of after the job has waited in the queue.

How the two are kept from fighting each other:
- The TSV is always the source of truth. `--use-txt` never regenerates it from the text file; it only writes the **cells you changed** in the text file (compared with a hidden snapshot taken when the text file was made). Anything changed in the TSV by hand (LibreOffice) is kept.
- Same cell changed to different values in both files: nothing is written and the conflict is listed.
- Without `--use-txt`, step 02 uses the TSV exactly as before. If the text file contains edits that are not in the TSV, step 02 **stops** and tells you, so edits are never silently ignored.
- A timestamped backup of the TSV (`*.tsv.bak-…`) is made before any edit is written, and the text file is refreshed after step 02 (`tsv_to_json.py` recalculates `runNum`).
- Rerunning step 01 rebuilds the TSV and the text file; the previous ones are kept as `*.prev`.
- `python helpers/view_tsv.py make <tsv> --force` throws away the text edits and rebuilds the text file from the TSV.

### Where everything goes (`$OUT_BASE/`, or `$OUT_BASE/<MAGNET>/` if `USE_MAGNET_FOLDER=1`)
| Folder | Made by | What |
|---|---|---|
| `dicoms_last/`, `helper/` | 01 | 1 DICOM per series and its quick conversion (only used for the TSV) |
| `summaries/` | 01–04 | `sub-X_ses-Y.tsv/.json`, `nordic_cmd_*.sh`, `layout_map_sub-X.tsv` |
| `dicoms/` | 02 | full DICOMs |
| `bids/` | 02, 03 | raw BIDS (mag + phase + NORDIC output next to them) |
| `derivatives/nordic/` | 04 | NORDIC-cleaned sessions |
| `bids_combined/` | 04 | fMRIPrep input for subjects with several sessions: `sub-X/ses-combined/{anat,func,fmap}`. Not made for a single session |
| `bids_sessions/` | 04 | `sub-X/anat` + `sub-X/ses-N/{func,fmap}` |
| `derivatives/91k_fmriprep_*`, `91k_xcpd_*` | 05 | pipeline outputs; XCP-D is the deliverable |
| `jobs/`, `logs/` | all | child job IDs, NORDIC and postnordic logs |

### One session vs several
- **Several sessions:** step 4 builds `bids_combined/` (all sessions merged into `ses-combined`, runs renumbered) and `bids_sessions/`, and step 5 reads `bids_combined/`.
- **One session:** there is nothing to combine, so step 4 skips both, writes IntendedFor straight into `derivatives/nordic/sub-X/ses-Y/`, and step 5 reads `derivatives/nordic` with the real session name kept (this is also what the lab's own workflow does). Set `SKIP_LAYOUTS_FOR_ONE_SESSION=0` in `config.sh` to always build both.
- `--layout auto` (default) picks between these for you. Override with `--layout nordic`, `combined` or `sessions`: `./run_subject.sh SUB001 01 --from 5 --layout nordic`.

### 3T vs 7T
Only needed if your study has both. The `MAGNET` argument selects it (and, if you set `USE_MAGNET_FOLDER=1` in `config.sh`, names the output sub-folder `3T/` or `7T/` so the two field strengths don't mix. The default is no such folder.) 7T additionally denoises the MP2RAGE and starts bias-field correction in step 03 (needs `helpers/Bias_field_script_job.m`, not included here), and step 04 copies in the final T1w/T2w and fabricates the AP fieldmap (7T fmaps are PA only).

### Using it on an existing study folder (e.g. only steps 4–5)
If the earlier steps were done elsewhere, point the config at that folder and run only the steps you need:
```bash
export OUT_BASE="/path/to/study"        # the folder that CONTAINS bids/, e.g. /path/to/study/bids/sub-X/ses-Y/func
./run_subject.sh SUB001 01 --from 4 --to 5
```
(`USE_MAGNET_FOLDER` stays at its default `0`, so there is no `3T/` or `7T/` folder inside it.) The wrapper will add `summaries/`, `jobs/`, `logs/`, `bids_combined/`, `bids_sessions/` and `derivatives/nordic/` inside that folder. Step 4 expects `bids/sub-X/ses-Y/{func,fmap,anat}` with the NORDIC output files next to the originals (the layout step 3 produces); if your NORDIC output already lives in `derivatives/nordic/`, check step 4's `postnordic.py` stage first.

## Updating to a newer version
```bash
cd cdniproc_wrapper
git config pull.rebase false      # one-time: silences a harmless "divergent branches" hint
git pull --autostash              # saves your edits (e.g. to config.sh), pulls, puts them back
grep -n "YOUR_" config.sh         # prints nothing if your settings survived
```
Your `logs/` contents and your output data are never touched by a pull (logs are git-ignored and outputs live in `OUT_BASE`). `config.sh` is the one file you edit, so it is the one that can conflict:
- If the update and your edits changed the same line, git prints `CONFLICT` and marks the file with `<<<<<<<`, `=======`, `>>>>>>>`. Open it, keep the lines you want, delete the marker lines, save.
- To discard your edits and take the repo's version: `cp config.sh ~/config.sh.mine && git restore config.sh && git pull`, then re-enter your settings (compare with `diff ~/config.sh.mine config.sh`).
- Stuck in a half-finished conflict (`Pulling is not possible because you have unmerged files`)? `git checkout HEAD -- config.sh` clears it. To reset everything to GitHub's version: `git fetch && git reset --hard origin/main` (discards all local edits to tracked files).
New settings arrive with defaults, so an older `config.sh` keeps working.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Job ran but no log files | The log folder did not exist. `mkdir -p logs`. `run_subject.sh` passes absolute log paths so this only bites bare `sbatch` runs from another directory |
| Job fails immediately | Submitted from a different directory so `config.sh` was not found. `cd` here first |
| Step 01 finds no DICOMs / wrong S3 URL | The log line `pulling last DICOM of each series: s3://…` shows the path used. Compare with `s3cmd ls s3://<bucket>/…` and fix `S3_BUCKET` / `S3_DICOM_PATH` in `config.sh` |
| Step 02 stops: "the text view has edits that are NOT in the TSV" | You edited the `.view.txt` but did not pass `--use-txt`. Rerun with `--use-txt`, or discard the text edits with `python helpers/view_tsv.py make <tsv> --force` |
| `git pull` refuses because of local changes / unmerged files | See "Updating to a newer version" above |
| `OUT_BASE contains bids/sub-/ses-` | Pick another `OUT_BASE` (the lab NORDIC scripts split paths on those words) |
| "no `*_part-mag_*` func files" (02) | TSV has no `M`/`P` rows for the rest runs; fix and rerun 02 |
| `Invalid account or account/partition` in step 03 | The lab NORDIC script hard-codes `-p msismall`. Set `RES_NORDIC_PART` in `config.sh` to a partition your account can use, rerun 03 |
| NORDIC `TIMEOUT` | Raise `RES_NORDIC_TIME` in `config.sh` (lab default 1.5 h), rerun 03 then 04 |
| `NORDIC FAILED` (04) | Read `$OUT_BASE/logs/nordic/*.err` (`$OUT_BASE/<MAGNET>/logs/…` if `USE_MAGNET_FOLDER=1`), rerun 03 for that session, rerun 04 |
| BIDS validator/fMRIPrep rejects `bids_sessions` | Set `SESSIONS_ANAT="session"` and rerun 04 (`bids_combined` is unaffected) |

## Notes
- The `*_dev.py` scripts and `archive/` in `cdniproc_v2.0` are older/experimental and are not used. The lab's `combineSessions_gpt3.py` is not used either: it would skip the NORDIC functional files.
- Not covered yet: PCM / template matching (the lab's `submit_PCM.sh`, `submit_TM.sh`).
- `config.sh` ships with placeholders (`YOUR_SLURM_ACCOUNT`, `YOUR_S3_BUCKET`). `run_subject.sh` refuses to run until you replace them. `STUDY_NAME` is optional and only names a folder inside `OUT_BASE`.
