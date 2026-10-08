#!/bin/bash
# ============================================================================
# config.sh  -  the ONLY file with paths and versions. Every step sources it.
# Edit this once per person / per study; you should not need to edit the steps.
#
# Sections:  1 who/where   2 lab code   3 software   4 pipeline versions
#            5 SLURM resources per step   6 fMRIPrep + XCP-D flags   7 choices
# Resources (section 5) and the account are applied by run_subject.sh. If you run a
# step with a bare `sbatch 0X_*.sh` instead, the defaults written in that script's
# #SBATCH header are used.
# ============================================================================

# ---- 1. who / where -----------------------------------------------------------
export SLURM_ACCOUNT="YOUR_SLURM_ACCOUNT"    # EDIT: your MSI account. run_subject.sh gives it to every job; child jobs (NORDIC, bias-field) inherit it
export SBATCH_ACCOUNT="${SLURM_ACCOUNT}"     # so sbatch calls made inside jobs use it too
export DEFAULT_MAGNET="${DEFAULT_MAGNET:-3T}"   # used when you leave MAGNET out of a command. Most studies have only one field strength: set it to 7T if that is yours
export S3_BUCKET="YOUR_S3_BUCKET"           # EDIT: bucket name only (the part right after s3://)
# Where one session's DICOMs are inside the bucket. {SUB}, {SES} and {MAGNET} are filled in for each run
# ({SUB} = subject ID, {SES} = session label, {MAGNET} = 3T or 7T).
#   DICOMs at s3://<bucket>/dicoms/SUB001_01/                    ->  'dicoms/{SUB}_{SES}/'              (default)
#   DICOMs at s3://<bucket>/mystudy/3T/dicoms/sub-SUB001_ses-01/ ->  'mystudy/{MAGNET}/dicoms/sub-{SUB}_ses-{SES}/'
# (If the first folder is the bucket itself, put it in S3_BUCKET and drop it from the pattern.)
export S3_DICOM_PATH='dicoms/{SUB}_{SES}/'
export STUDY_NAME="${STUDY_NAME:-}"          # OPTIONAL: short study name (e.g. mystudy); becomes a folder in OUT_BASE below. Leave empty to skip it

# Everything is written under  $OUT_BASE/<MAGNET>/   (MAGNET = 3T or 7T)
#   with STUDY_NAME="mystudy":  /scratch.global/<you>/projects/mystudy/data/processing
#   with STUDY_NAME="":         /scratch.global/<you>/projects/data/processing
# Or set OUT_BASE yourself to any location. Keep the word "bids" and the strings "sub-" / "ses-" OUT of
# this path (and of STUDY_NAME) - the lab's NORDIC scripts find their folders by splitting the path on those words.
export OUT_BASE="${OUT_BASE:-/scratch.global/${USER}/projects/${STUDY_NAME:+${STUDY_NAME}/}data/processing}"

# ---- 1b. our code --------------------------------------------------------------
export CODE_DIR="${CODE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"   # = the folder this file is in; no edit needed
export HELPERS="${CODE_DIR}/helpers"

# ---- 2. shared lab code (always the current version on the server) ------------
export LAB_UTILS="/projects/standard/faird/shared/code/internal/utilities"
export CDNIPROC="${LAB_UTILS}/cdniproc_v2.0"
export NORDIC_WRAPPER="${LAB_UTILS}/Dcm2bids3_NORDIC_wrapper"   # runnordic.m lives here (called by nordicsbatch_new.sh)
export S3_LAST_DICOMS="${LAB_UTILS}/MSI-utilities/s3_get_last_dicoms/s3_get_last_dicoms.sh"
# sbatch script NORDIC jobs run. make_nordic_cmds.py hard-codes the shared one (1.5 h limit, msismall).
# To use a modified copy (e.g. longer -t), point this at it; 03_run_nordic.sh swaps it into the command list.
export NORDIC_SBATCH="${CDNIPROC}/code/nordicsbatch_new.sh"

# ---- 3. software --------------------------------------------------------------
export CONDA_LOADER="/projects/standard/faird/shared/code/external/envs/miniconda3/load_miniconda3.sh"
export CONDA_ENV="py11"                      # needs dcm2bids (v3), dcm2niix, pandas, nibabel
export FSL_MODULE="fsl/5.0.10"
export MATLAB_MODULE="matlab/R2019a"
export LAYNII_DIR="/projects/standard/bart/shared/projects/7Tpiloting/anat_testing/scripts/LayNii"   # 7T only (LN_MP2RAGE_DNOISE)

# ---- 4. pipelines (kept at the versions this study already used) --------------
# Containers are read from $PIPELINES_DIR/fmriprep/fmriprep_<ver>.sif and xcp_d/xcp_d_<ver>.sif
# - `ls $PIPELINES_DIR/fmriprep $PIPELINES_DIR/xcp_d` shows what is installed.
# Pick the versions once for the whole study and do not change them part-way.
export FMRIPREP_VERSION="25.1.3"             # newest on server per Module 9: 25.2.2
export XCPD_VERSION="0.11.1"                 # newest on server per Module 10: 26.2.0rc1
export CIFTI_SPACE="91k"
export PIPELINES_DIR="/projects/standard/faird/shared/code/external/pipelines"
export FS_LICENSE="/projects/standard/faird/shared/code/external/utilities/freesurfer_license/license.txt"

# ---- 5. SLURM resources per step ----------------------------------------------
# CPUS, MEM (e.g. 32G), TIME (H:MM:SS) and PARTITION for each step script.
# Steps 01-04 are light; 05 (fMRIPrep + XCP-D) is the heavy one.
# NORDIC jobs that step 03 submits use the lab's nordicsbatch_new.sh (1.5 h, 80 GB, msismall);
# to change those, copy that script, edit it and set NORDIC_SBATCH (section 2).
PART_STD="ag2tb,agsmall,aglarge"
RES_01_CPUS=4;   RES_01_MEM=16G;  RES_01_TIME=1:00:00;   RES_01_PART="${PART_STD}"
RES_02_CPUS=8;   RES_02_MEM=32G;  RES_02_TIME=8:00:00;   RES_02_PART="${PART_STD}"
RES_03_CPUS=8;   RES_03_MEM=64G;  RES_03_TIME=4:00:00;   RES_03_PART="${PART_STD}"
RES_04_CPUS=4;   RES_04_MEM=32G;  RES_04_TIME=12:00:00;  RES_04_PART="${PART_STD}"
RES_05_CPUS=24;  RES_05_MEM=650G; RES_05_TIME=50:00:00;  RES_05_PART="${PART_STD},msismall"

# NORDIC child jobs (submitted by step 03; sized here, not by RES_03) ----------------------------------------------
# The lab's nordicsbatch_new.sh hard-codes "-p msismall" (1.5 h, 80 GB, 2 CPUs). If your account
# cannot use that partition you get "Invalid account or account/partition combination".
# These values override the script's #SBATCH lines when step 03 submits the jobs.
RES_NORDIC_PART="ag2tb,agsmall,aglarge"
RES_NORDIC_CPUS=2
RES_NORDIC_MEM=80G
RES_NORDIC_TIME=1:30:00

# ---- 6. fMRIPrep and XCP-D flags (step 05) ----------------------------------
# One flag (and its value) per line. Defaults = the lab's abcd-mode settings that this study used.
# Step 05 always adds these itself, do NOT list them here: --fs-license-file, --participant-label,
# -w (work dir) and the input/output folders.
# The thread counts below follow the CPUs you request in section 5 so the two stay in sync.
FMRIPREP_FLAGS=(
    --output-spaces MNI152NLin6Asym:res-2:res-native
    --project-goodvoxels
    --use-syn-sdc
    --skull-strip-fixed-seed
    --random-seed 1
    --omp-nthreads 3
    --cifti-output "${CIFTI_SPACE}"
    --bold2anat-init auto
    -vv
)
XCPD_FLAGS=(
    --mode abcd
    --band-stop-min 12 --band-stop-max 18 --motion-filter-type notch
    --motion-filter-order 4
    --lower-bpf 0.009
    --smoothing 0
    --linc-qc
    --min-time 0
    --omp-nthreads 3
    --nprocs "${RES_05_CPUS}"
    --warp-surfaces-native2std
    -vv
)
# Post-XCP-D tools (motion conversion, interpolation of high-motion frames)
export XCPD_MOTION="${LAB_UTILS}/xcpd2dcanmotion"
export INTERP="${LAB_UTILS}/interpolate_noise_for_timeseries"
export COMPARE="/projects/standard/faird/shared/code/internal/analytics/compare_matrices_to_assign_networks/"
export WB="/projects/standard/faird/shared/code/external/utilities/workbench/1.4.2/workbench/bin_rh_linux64/wb_command"

# ---- 7. choices ----------------------------------------------------------------
# Which script writes IntendedFor into the fmap JSONs:
#   jsbr = helpers/IntendedFor_JSBR.py  (what our SOP used; ShimSetting + timing aware)
#   lab  = cdniproc_v2.0/tools/IntendedFor_new.py (shim matching)
# Note: the 7T "fake AP" step always uses jsbr first (it needs PA-only fmaps to be handled).
export INTENDEDFOR_METHOD="jsbr"

# Reviewing the summary TSV (step 01 -> 02). Default 0: step 02 uses the TSV exactly as it is (edit it in LibreOffice
# or VS Code, the original way). 1 = also copy your edits from summaries/<sub>_<ses>.view.txt into the TSV first.
# Normally you set this per run with  ./run_subject.sh ... --use-view  instead of here.
export USE_VIEW_EDITS="${USE_VIEW_EDITS:-0}"

# Where anatomicals go in bids_sessions:  subject = sub-X/anat   |   session = sub-X/ses-N/anat
export SESSIONS_ANAT="subject"
