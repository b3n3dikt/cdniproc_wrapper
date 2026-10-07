#!/bin/bash -l
#SBATCH -J 03_nordic
#SBATCH -c 8
#SBATCH --mem=64G
#SBATCH -t 4:00:00
#SBATCH -p ag2tb,agsmall,aglarge
#SBATCH -o logs/%x_%j.out
#SBATCH -e logs/%x_%j.err
#
# STEP 3 - NORDIC thermal-noise denoising (and, for 7T, anatomical denoising + bias-field correction).
#
# Usage:  sbatch 03_run_nordic.sh <SUB> <SES> [MAGNET]      (MAGNET omitted = DEFAULT_MAGNET in config.sh)
#
# What it does
#   1. make_nordic_cmds.py   looks at <ROOT>/bids/sub-X/ses-Y/func, pairs every *_part-mag_* with its
#                            *_part-phase_*, counts the no-RF "noise" volumes at the end of each run, and
#                            writes one `sbatch nordicsbatch_new.sh ...` line per mag/phase pair (= per echo).
#   2. submits those lines   each NORDIC job writes  *_task-restNORDIC_*_bold.nii.gz (noise volumes trimmed)
#                            next to the originals in bids/sub-X/ses-Y/func.
#   3. 7T only               helpers/prep_7T_anat.sh  (MP2RAGE denoise + bias-field correction jobs)
#
# This step does NOT wait for the NORDIC jobs. It records their job IDs in
#   <ROOT>/jobs/sub-X_ses-Y.jobids      and step 04 waits for them.
set -Eeuo pipefail

CODE_DIR="${CODE_DIR:-${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}}"
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"

[[ $# -ge 2 ]] || die "Usage: sbatch $(basename "$0") <SUB> <SES> [MAGNET]"
SUB="$1"; SES="$2"; set_magnet "${3:-${DEFAULT_MAGNET}}"
TAG="sub-${SUB}_ses-${SES}"
FUNC_DIR="${RAW_BIDS}/sub-${SUB}/ses-${SES}/func"
JF="$(jobfile "${SUB}" "${SES}")"
CMDS="${SUMMARY_DIR}/nordic_cmd_${TAG}.sh"

[[ -d "${FUNC_DIR}" ]] || die "${FUNC_DIR} not found - run step 02 first"
load_env
need_cmd python

# NORDIC jobs write their logs to ./logs/nordic/ relative to where they are submitted, and
# make_nordic_cmds.py writes its command file one level above bids/  ->  work from ROOT.
cd "${ROOT}"
mkdir -p logs/nordic
: > "${JF}"                                   # forget job IDs from any previous attempt

log "1/3 writing NORDIC commands"
python "${CDNIPROC}/tools/make_nordic_cmds.py" "${FUNC_DIR}"
[[ -f "${ROOT}/nordic_cmd_${TAG}.sh" ]] || die "make_nordic_cmds.py did not write ${ROOT}/nordic_cmd_${TAG}.sh"
mv "${ROOT}/nordic_cmd_${TAG}.sh" "${CMDS}"
if [[ "${NORDIC_SBATCH}" != "${CDNIPROC}/code/nordicsbatch_new.sh" ]]; then
    log "using custom NORDIC sbatch script: ${NORDIC_SBATCH}"
    sed -i "s#${CDNIPROC}/code/nordicsbatch_new.sh#${NORDIC_SBATCH}#g" "${CMDS}"
fi
echo "    $(wc -l < "${CMDS}") NORDIC job(s) in ${CMDS}"

log "2/3 submitting NORDIC jobs"
while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    # the lines are plain `sbatch <script> <args>`; add --parsable so we can record the job ID
    # override the lab script's hard-coded partition/time/memory with the values from config.sh
    submit_and_record "${JF}" -A "${SLURM_ACCOUNT}" -p "${RES_NORDIC_PART}" -c "${RES_NORDIC_CPUS}" \
        --mem="${RES_NORDIC_MEM}" -t "${RES_NORDIC_TIME}" ${line#sbatch }
done < "${CMDS}"

if [[ "${MAGNET}" == "7T" ]]; then
    log "3/3 7T anatomicals: MP2RAGE denoise + bias-field correction"
    bash -l "${HELPERS}/prep_7T_anat.sh" "${SUB}" "${SES}"
else
    log "3/3 3T: no extra anatomical processing"
fi

log "DONE submitting. Job IDs in ${JF}. Watch with:  squeue -u \$USER"
