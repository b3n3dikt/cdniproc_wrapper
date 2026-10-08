#!/bin/bash -l
#SBATCH -J 02_bids
#SBATCH -c 8
#SBATCH --mem=32G
#SBATCH -t 8:00:00
#SBATCH -p ag2tb,agsmall,aglarge
#SBATCH -o logs/%x_%j.out
#SBATCH -e logs/%x_%j.err
#
# STEP 2 - DICOMs -> BIDS (dcm2bids), using the reviewed TSV from step 1.
#
# Usage:  sbatch 02_convert_to_bids.sh <SUB> <SES> [MAGNET]      (MAGNET omitted = DEFAULT_MAGNET in config.sh)
#
# What it does
#   1. tsv_to_json.py   turns summaries/sub-<SUB>_ses-<SES>.tsv into the dcm2bids config .json
#   2. s3cmd sync       downloads the FULL DICOM set for the session
#   3. dcm2bids         converts to  <ROOT>/bids/sub-<SUB>/ses-<SES>/{anat,fmap,func}
#                       (functional runs come out as separate mag and phase files - NORDIC needs both)
set -Eeuo pipefail

CODE_DIR="${CODE_DIR:-${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}}"
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"

[[ $# -ge 2 ]] || die "Usage: sbatch $(basename "$0") <SUB> <SES> [MAGNET]"
SUB="$1"; SES="$2"; set_magnet "${3:-${DEFAULT_MAGNET}}"
TAG="sub-${SUB}_ses-${SES}"
TSV="${SUMMARY_DIR}/${TAG}.tsv"
CFG="${SUMMARY_DIR}/${TAG}.json"
S3_DIR="$(s3_dicom_dir "${SUB}" "${SES}")"      # built from S3_BUCKET + S3_DICOM_PATH in config.sh

[[ -f "${TSV}" ]] || die "${TSV} not found - run step 01 first"
load_env
need_cmd python dcm2bids dcm2niix s3cmd

# The TSV is what tsv_to_json.py reads. summaries/<sub>_<ses>.view.txt is an optional text version of it.
VIEW_PY="${HELPERS}/view_tsv.py"
if [[ "${USE_TXT_EDITS:-0}" == "1" ]]; then
    log "0/3 --use-txt: copying your edits from the text view into the TSV (edits made directly in the TSV are kept)"
    python "${VIEW_PY}" apply "${TSV}" || die "could not apply the text-view edits - see above. Nothing was changed."
else
    rc=0; python "${VIEW_PY}" status "${TSV}" || rc=$?
    (( rc == 0 )) || die "the text view has edits that are NOT in the TSV. Rerun with --use-txt to use them, or undo them (python helpers/view_tsv.py make ${TSV} --force discards them). Not continuing, so nothing is silently ignored."
fi

log "1/3 TSV -> dcm2bids config"
python "${CDNIPROC}/tsv_to_json.py" "${TSV}"
[[ -f "${CFG}" ]] || die "tsv_to_json.py did not create ${CFG}"
# tsv_to_json.py rewrites the TSV (recalculates runNum): refresh the text view so it matches what was used
python "${VIEW_PY}" make "${TSV}" --force

log "2/3 syncing full DICOMs from ${S3_DIR}"
mkdir -p "${DICOM_DIR}/${TAG}" "${RAW_BIDS}"
s3cmd sync --recursive --no-check-md5 --skip-existing "${S3_DIR}" "${DICOM_DIR}/${TAG}/"

log "3/3 dcm2bids"
dcm2bids -d "${DICOM_DIR}/${TAG}" -p "${SUB}" -s "${SES}" -c "${CFG}" -o "${RAW_BIDS}"

echo
log "DONE. What was converted:"
for dt in anat fmap func; do
    n=$(ls "${RAW_BIDS}/sub-${SUB}/ses-${SES}/${dt}/"*.nii.gz 2>/dev/null | wc -l || true)
    printf '    %-5s %s nifti file(s)\n' "${dt}" "${n}"
done
ls "${RAW_BIDS}/sub-${SUB}/ses-${SES}/func/"*part-mag*.nii.gz >/dev/null 2>&1 \
    || log "WARNING: no *_part-mag_* func files. NORDIC (step 03) needs mag+phase pairs - check the 'MP' and 'label' columns in ${TSV}."
