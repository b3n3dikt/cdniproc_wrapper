# SOP: cdniproc_wrapper (DICOM to XCP-D pipeline)

DICOM → BIDS → NORDIC → fMRIPrep → XCP-D     |     Version 3     |     2026-10-07

**Code:** the `cdniproc_wrapper` repository (clone it anywhere on the server). README.md in the repository has the same material in short form.

## 1. What this pipeline does

Five numbered scripts take a session from raw DICOMs on S3 to cleaned fMRI data (XCP-D output). Each script runs on SLURM. You can run them one at a time, or chain them for a whole subject with `run_subject.sh`. Every path, version, SLURM account, resource request and fMRIPrep / XCP-D flag lives in one file, `config.sh`, so the step scripts do not need editing.

The workflow in short: **pull the folder, edit config.sh, run the steps you want.** The wrapper does not reimplement the lab code. It calls the current shared versions on the server (cdniproc_v2.0, runnordic.m from Dcm2bids3_NORDIC_wrapper, the fMRIPrep and XCP-D containers) and handles the glue: paths, job chaining, waiting for NORDIC, building the final datasets. It replaces the older cdniproc_reprocess workflow.

| # | Script | Run per | What it does |
|---|---|---|---|
| 01 | `01_pull_and_make_tsv.sh` | session | Pull the last DICOM of each series from S3, run dcm2niix, build the summary TSV. You review the TSV. |
| 02 | `02_convert_to_bids.sh` | session | TSV → dcm2bids config, sync the full DICOMs, run dcm2bids. |
| 03 | `03_run_nordic.sh` | session | Write and submit NORDIC jobs (mag + phase → denoised magnitude). 7T: also MP2RAGE denoise and bias-field jobs. |
| 04 | `04_clean_and_layout.sh` | subject | Wait for NORDIC, check it, 7T extras, build bids_combined and bids_sessions, write IntendedFor. |
| 05 | `05_fmriprep_xcpd.sh` | subject | fMRIPrep, XCP-D, motion and interpolation conversions, template-matching input folder. |

**Terms.** **SUB** = subject ID without "sub-" (e.g. SUB001). **SES** = session label without "ses-", as it appears in the S3 folder name (numeric sessions two digits, e.g. 01; labels such as 3T1 or 7T1 are fine). **MAGNET** = 3T or 7T; optional, if you leave it out the DEFAULT_MAGNET from config.sh is used (3T unless you change it), so a study with a single field strength can ignore it. Where the DICOMs sit in S3 is set by S3_BUCKET and S3_DICOM_PATH in config.sh.

## 2. Getting started

- [ ] Clone the repository on the MSI server: `git clone https://github.com/b3n3dikt/cdniproc_wrapper.git && cd cdniproc_wrapper`
- [ ] Edit `config.sh` (section 3 explains each setting). At minimum: SLURM_ACCOUNT, S3_BUCKET, OUT_BASE, CONDA_ENV.
- [ ] Create the log folder: `mkdir -p logs`
- [ ] 7T only: copy `Bias_field_script_job.m` from the old cdniproc_reprocess/pCodePath/ into helpers/ (not included in the repository).
- [ ] **Always** `cd` into the repository folder before running anything. `run_subject.sh` writes logs to its logs/ folder; SLURM silently drops output if the log folder is missing.

**Pre-flight** (each should print a path or "ok"):

```bash
source /projects/standard/faird/shared/code/external/envs/miniconda3/load_miniconda3.sh; conda activate py11
which dcm2bids dcm2niix s3cmd
python -c "import pandas, nibabel; print('ok')"
```

## 3. Configuring a study: config.sh

Everything you may need to change is in `config.sh`, which is divided into numbered sections. config.sh ships with placeholders (YOUR_SLURM_ACCOUNT, YOUR_S3_BUCKET) and run_subject.sh will not run until they are replaced. Other defaults are the settings the original study used.

| Setting | Section | What to set |
|---|---|---|
| SLURM_ACCOUNT | 1 | Your MSI group/account. run_subject.sh passes it to every job, so the scripts need no edits (they no longer contain an account). |
| DEFAULT_MAGNET | 1 | Field strength used when you leave MAGNET out of a command (3T by default). Set it to 7T if your study is all 7T. Only studies with both 3T and 7T data need to type MAGNET. |
| S3_BUCKET | 1 | Bucket name only (the part right after s3://). |
| S3_DICOM_PATH | 1 | Where one session's DICOMs are inside the bucket, as a template; {SUB}, {SES} and {MAGNET} are filled in for each run. Default `dicoms/{SUB}_{SES}/`. For s3://<bucket>/mystudy/3T/dicoms/sub-SUB001_ses-3T1/ use `mystudy/{MAGNET}/dicoms/sub-{SUB}_ses-{SES}/`. If the first folder is the bucket itself, put it in S3_BUCKET and drop it from the pattern. |
| STUDY_NAME | 1 | Optional short study name (e.g. mystudy). It becomes a folder in the default OUT_BASE; leave it empty to skip that folder. |
| OUT_BASE | 1 | Scratch location for all outputs. Default: /scratch.global/<you>/projects/<STUDY_NAME>/data/processing (the STUDY_NAME folder is skipped if empty), or set it to any path; everything is written under OUT_BASE/<MAGNET>/. It (and STUDY_NAME) must NOT contain the words "bids", "sub-" or "ses-" (the lab NORDIC scripts split paths on them). run_subject.sh refuses to start if it does. |
| CODE_DIR | 1 | Set automatically to the folder config.sh is in. No edit needed. |
| NORDIC_SBATCH | 2 | Only if you need a modified copy of the lab NORDIC script. Partition, time and memory are set with RES_NORDIC_* instead. |
| CONDA_ENV | 3 | Environment with dcm2bids v3, dcm2niix, pandas, nibabel. The lab uses py11. |
| FMRIPREP_VERSION, XCPD_VERSION, CIFTI_SPACE | 4 | Container versions (`ls $PIPELINES_DIR/fmriprep $PIPELINES_DIR/xcp_d` shows what is installed). Pin them for the whole study and do not change them part-way. |
| RES_01 … RES_05 | 5 | CPUS, MEM, TIME and PART (partition) for each step script, e.g. `RES_05_MEM=650G`. Defaults are what the study used. Applied by run_subject.sh. |
| FMRIPREP_FLAGS, XCPD_FLAGS | 6 | The fMRIPrep and XCP-D options, one flag per line. Defaults are the lab's abcd-mode settings. Add, remove or change flags here. Step 05 itself always adds --fs-license-file, --participant-label, -w and the input/output folders, so do not list those. The default --nprocs follows RES_05_CPUS. |
| INTENDEDFOR_METHOD | 7 | jsbr (our script, shim + timing aware) or lab (IntendedFor_new.py, shim matching). Keep jsbr unless you have a reason. |
| SESSIONS_ANAT | 7 | subject (anat at sub-X/anat in bids_sessions) or session (anat inside each ses-*). Use session if the BIDS validator or fMRIPrep objects. |

Two study-specific files outside config.sh: `helpers/label_rules.csv` (series names in your protocol that the automatic labeller does not recognise; one line each: magnet, lookfor, label such as rest or fmap; anything left unlabelled is not converted) and `helpers/dataset_description.json` (study Name, Authors, Funding, License).

**Resources and bare sbatch.** run_subject.sh applies the account and the RES_ values from config.sh when it submits each job. A bare `sbatch 0X_*.sh` ignores them and uses the defaults in that script's #SBATCH header. To run a single step with your config, use `./run_subject.sh SUB [MAGNET] SES --from N --to N`. NORDIC jobs that step 03 submits are sized by RES_NORDIC_* (partition, CPUs, memory, time), not by RES_03; these override the lab nordicsbatch_new.sh, which hard-codes -p msismall.

**Study requirements.** The functional runs must be saved with both magnitude and phase (NORDIC needs the pair for every run), and the DICOM folders on S3 must follow the SUB_SES naming above. If the new study has different series names, expect to add to label_rules.csv after the first TSV (section 5.1).

**Validate with one subject first.** Run one subject with one session, step by step (section 4.2), before running the whole study, and check the outputs listed in section 5.4.

## 4. How to run

### 4.1 Whole subject, chained

Run on the login node from the repository folder. The script only submits SLURM jobs; each job starts only if the one before it finished OK. Sessions run in parallel.

Example: subject **SUB001**, scanner **3T**, two sessions **01** and **02**.

```bash
cd cdniproc_wrapper

# SUB001 = subject ID | 3T = MAGNET (field strength) | 01 02 = session labels (ses-01 and ses-02)

# Step 1 only (--to 1): pull the DICOMs and make one summary TSV per session, so you can review them
./run_subject.sh SUB001 3T 01 02 --to 1

#   ...open and fix the TSVs here (section 5.1)...

# Steps 2-5 (--from 2): convert to BIDS, NORDIC, build datasets, fMRIPrep + XCP-D
./run_subject.sh SUB001 3T 01 02 --from 2

# Same, but MAGNET left out (uses DEFAULT_MAGNET from config.sh, 3T by default)
./run_subject.sh SUB001 01 02 --from 2
```

Options: `--from N` and `--to N` choose which steps to submit (1–5); `--layout combined|sessions` chooses the fMRIPrep input (default combined). Watch jobs with `squeue -u $USER`. Logs are in the repository's logs/ folder.

### 4.2 One step at a time

```bash
# SUB001 = subject, 01 / 02 = sessions, 3T = MAGNET (optional: leave it out to use DEFAULT_MAGNET)
sbatch 01_pull_and_make_tsv.sh SUB001 01 3T      # steps 1-3: once per session
sbatch 02_convert_to_bids.sh   SUB001 01 3T
sbatch 03_run_nordic.sh        SUB001 01 3T
sbatch 04_clean_and_layout.sh  SUB001 3T 01 02   # steps 4-5: once per subject, after 02 and 03 finished for every session
sbatch 05_fmriprep_xcpd.sh     SUB001 3T         # add "sessions" at the end to use bids_sessions
```

When running by hand like this, always do it from the repository folder so the relative log paths work. Remember that bare sbatch uses the #SBATCH header defaults, not the RES_ values from config.sh.

## 5. The steps in detail

### 5.1 Step 01: pull DICOMs and make the summary TSV

- Copies the last DICOM of every series from S3 (a small "mini" copy of the session).
- Runs dcm2niix on them (`tools/convert_helper.py`) into helper/tmp_dcm2bids/.
- Builds the summary TSV (`nii_init_gpt5.py`) with one row per series and an automatic BIDS label guess, then applies helpers/label_rules.csv.

**Output:** `OUT_BASE/<MAGNET>/summaries/sub-<SUB>_ses-<SES>.tsv`

**Two ways to fix the TSV; both end with the same TSV, which is what step 02 reads.** (1) The original way: open the .tsv in LibreOffice or any editor, fix it, save, run step 02. (2) The text-file way, no LibreOffice: step 01 also writes `sub-<SUB>_ses-<SES>.view.txt` next to the TSV, a short aligned table with a CHECKS section (unlabelled series, rest runs missing a magnitude or phase partner, fmaps without PEdir). Edit the columns marked * (label, MP, acq, PEdir, inv, nEcho; write an empty cell as a single dot). Then preview with `python helpers/view_tsv.py status <tsv>` and run step 02 with `--use-view` (`./run_subject.sh SUB [MAGNET] SES --from 2 --use-view`).

- The TSV stays the source of truth. --use-view writes only the cells you changed in the text file (compared with a hidden snapshot taken when the view was made); anything changed in the TSV by hand is kept. If the same cell was changed differently in both, nothing is written and the conflict is listed.
- Without --use-view step 02 uses the TSV as before. If the text file holds edits that are not in the TSV, step 02 stops and says so, so edits are never silently ignored.
- A backup of the TSV (.tsv.bak-<time>) is made before edits are written. `python helpers/view_tsv.py make <tsv> --force` discards text edits and rebuilds the view. Rerunning step 01 keeps the previous TSV and view as .prev.

**You must review the TSV** (use the .view.txt to check it, edit the .tsv) before step 02:

- **label**: rest, fmap, t1w, t2w, ... An empty label means the series is not converted. Edit the cell directly.
- **MP**: M = magnitude, P = phase. Every rest run needs both.
- **PEdir**: AP or PA for fieldmaps.  **EchoNumber**: number of echoes.
- Series names the labeller misses (e.g. 7T mbep2d_bold, REV): add a line to helpers/label_rules.csv so it is automatic next time.

**If it fails:** "convert_helper.py did not create ..." means the session label does not match S3 or the S3 pull found nothing. Look in dicoms_last/.

### 5.2 Step 02: convert to BIDS

- Turns the reviewed TSV into the dcm2bids config (`tsv_to_json.py` writes sub-<SUB>_ses-<SES>.json next to the TSV).
- Syncs the full DICOMs from S3 into dicoms/.
- Runs dcm2bids into bids/sub-<SUB>/ses-<SES>/{anat,fmap,func}.

**Check:** func contains both *_part-mag_* and *_part-phase_* files. The script prints a warning if there are no part-mag files; fix the label/MP cells in the TSV and rerun 02.

### 5.3 Step 03: NORDIC

- `make_nordic_cmds.py` pairs every magnitude run with its phase run, counts the no-RF noise volumes at the end, and writes one sbatch line per mag/phase pair (per echo).
- Those jobs are submitted (`nordicsbatch_new.sh` > runnordic.m). Each writes a *_task-restNORDIC_*_bold.nii.gz next to the originals in bids/.
- 7T only: helpers/prep_7T_anat.sh denoises the MP2RAGE (LayNii) and submits bias-field-correction jobs.

Step 03 only **submits** the NORDIC jobs and does not wait. Job IDs are saved in jobs/sub-X_ses-Y.jobids and step 04 waits for them. The command list is saved as summaries/nordic_cmd_*.sh. NORDIC errors are in OUT_BASE/<MAGNET>/logs/nordic/*.err.

### 5.4 Step 04: check NORDIC and build the final datasets

Run once per subject, after every session has finished steps 02 and 03.

- Waits for the NORDIC and bias-field jobs.
- `postnordic.py` verifies each magnitude run has a NORDIC run and moves the NORDIC files to derivatives/nordic/sub-X/ses-Y/. It stops with "NORDIC FAILED for session(s) ..." if any run is missing.
- 7T only: copies in the final T1w/T2w, writes a provisional IntendedFor, and makes a fake forward-PE (AP) fieldmap from the functional data (7T fieldmaps are PA only).
- `make_layouts.py` builds two datasets from derivatives/nordic:
  - `bids_combined/`  sub-X/ses-combined/{anat,func,fmap}, runs renumbered 01…N across sessions. This is the fMRIPrep input.
  - `bids_sessions/`  sub-X/anat plus sub-X/ses-N/{func,fmap}, sessions kept separate.
- Writes IntendedFor into the fieldmap JSONs of both datasets (file names change when sessions are combined or split, so this is redone).

**Checks:**

```bash
cd $OUT_BASE/3T
find bids_combined/sub-SUB -name "*.nii.gz" | sort
grep -L IntendedFor bids_combined/sub-SUB/ses-combined/fmap/*.json    # should print nothing
head -30 summaries/layout_map_sub-SUB.tsv                               # old name -> new name
```

**If NORDIC FAILED:** read logs/nordic/*.err, fix (often a timeout, see section 8), rerun 03 for that session, then rerun 04.

### 5.5 Step 05: fMRIPrep and XCP-D

- Runs fMRIPrep (singularity container) on bids_combined, then XCP-D in abcd mode with the lab's standard filter settings.
- Converts motion .hdf5 to .tsv and .mat, interpolates high-motion frames in the denoised CIFTI, and creates the folder the template-matching step reads.

**Final output:** `derivatives/91k_xcpd_<version>/output/<SUB>/`. fMRIPrep output is in derivatives/91k_fmriprep_<version>/. By default step 05 requests 24 CPUs, 650 GB and 50 h (RES_05_* in config.sh).

The versions and flags come from config.sh (FMRIPREP_VERSION, FMRIPREP_FLAGS, XCPD_VERSION, XCPD_FLAGS). Post-XCP-D tool locations are also in config.sh.

## 6. Where everything goes

All under `OUT_BASE/<MAGNET>/`

| Folder | Step | What |
|---|---|---|
| dicoms_last/, helper/ | 01 | One DICOM per series and its quick conversion (only used to build the TSV). |
| summaries/ | 01–04 | sub-X_ses-Y.tsv/.json, nordic_cmd_*.sh, layout_map_sub-X.tsv. |
| dicoms/ | 02 | Full DICOMs. |
| bids/ | 02, 03 | Raw BIDS: mag + phase, and the NORDIC output next to them. |
| derivatives/nordic/ | 04 | NORDIC-cleaned sessions (no phase, no noise volumes). |
| bids_combined/ | 04 | fMRIPrep input. |
| bids_sessions/ | 04 | Sessions kept separate. |
| derivatives/91k_fmriprep_*, 91k_xcpd_* | 05 | Pipeline outputs. XCP-D is the deliverable. |
| jobs/, logs/ | all | Child job IDs; postnordic, IntendedFor and NORDIC logs (logs/nordic/). |

## 7. 3T vs 7T (and studies with only one)

MAGNET only matters for studies that have both 3T and 7T data. Studies with one field strength can leave it out of every command and set DEFAULT_MAGNET in config.sh (3T by default). With both, give it explicitly. It also names the output sub-folder: OUT_BASE/3T/ or OUT_BASE/7T/.

- **7T:** step 03 also denoises the MP2RAGE and starts bias-field-correction jobs; step 04 copies the final T1w/T2w in, writes a provisional IntendedFor and fabricates the AP fieldmap. Confirm on the first 7T run: LayNii denoise ran, a *_dir-AP_*_epi file exists in derivatives/nordic/.../fmap, and the final T1w is named ..._run-NN_T1w.
- **3T:** none of that.

## 8. Troubleshooting

| Symptom | What to do |
|---|---|
| Job ran but there are no log files | The logs/ folder did not exist where sbatch was run. cd into the repository folder, mkdir -p logs, resubmit. run_subject.sh now passes absolute log paths so this is avoided. |
| Job fails instantly, nothing printed | You probably submitted from a different directory, so config.sh was not found. cd into the repository folder first. |
| "OUT_BASE contains bids/sub-/ses-" | Change OUT_BASE in config.sh to a path without those words. |
| "no *_part-mag_* func files" (02) | The TSV has no M/P rows for the rest runs. Fix label and MP, rerun 02. |
| "Invalid account or account/partition" in step 03 | The lab NORDIC script hard-codes -p msismall. Set RES_NORDIC_PART in config.sh to a partition your account can use and rerun 03. |
| NORDIC jobs TIMEOUT | Raise RES_NORDIC_TIME in config.sh (lab default 1.5 h), rerun 03 then 04. |
| "NORDIC FAILED" (04) | Check logs/nordic/*.err, rerun 03 for that session, rerun 04. |
| fmap JSON without IntendedFor | Rerun 04 (or `--from 4`). Try INTENDEDFOR_METHOD=lab to compare. |
| BIDS validator or fMRIPrep rejects bids_sessions | Set SESSIONS_ANAT="session" in config.sh and rerun 04. bids_combined is not affected. |
| Rerunning a step | Resubmit it, or `./run_subject.sh SUB [MAGNET] SES --from N --to N`. A later step needs every earlier step to be finished. |

## 9. Which lab code each step uses

All from `/projects/standard/faird/shared/code/internal/utilities/cdniproc_v2.0/`, matching the lab WORKFLOW.txt.

| Step | Script | Notes |
|---|---|---|
| 01 | tools/convert_helper.py, nii_init_gpt5.py | plus helpers/apply_label_rules.py (ours) |
| 02 | tsv_to_json.py, then dcm2bids |  |
| 03 | tools/make_nordic_cmds.py, code/nordicsbatch_new.sh, runnordic.m | runnordic.m from Dcm2bids3_NORDIC_wrapper |
| 04 | tools/postnordic.py; IntendedFor_JSBR.py (ours) or tools/IntendedFor_new.py | helpers/make_layouts.py is ours |
| 05 | fMRIPrep and XCP-D containers; xcpd2dcanmotion; tools/copy_files_to_temp.py | PCM / template matching is not part of this pipeline yet |

Do not use the *_dev.py scripts or the archive/ folder; they are older or experimental. The lab's combineSessions_gpt3.py is also not used, because it would skip the NORDIC functional files.

## 10. Sign-off for a new study or machine

- [ ] Steps 01–05 completed for one 3T session with no manual edits other than the TSV review.
- [ ] Two-session subject: ses-combined run numbers run 01…N across both sessions.
- [ ] IntendedFor present on every fmap JSON in both final datasets.
- [ ] 7T session completed (if the study has 7T).
- [ ] Outputs compared with a previous run of the same subject, if one exists.

Issues / notes:
