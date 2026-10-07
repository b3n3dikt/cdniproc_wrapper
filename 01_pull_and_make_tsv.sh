#!/bin/bash -l
#SBATCH -J 01_tsv
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 1:00:00
#SBATCH -p ag2tb,agsmall,aglarge
#SBATCH -o logs/%x_%j.out
#SBATCH -e logs/%x_%j.err
#
# STEP 1 - pull ONE DICOM per series from S3 and build the session summary TSV.
#
# Usage:  sbatch 01_pull_and_make_tsv.sh <SUB> <SES> [MAGNET]      (MAGNET omitted = DEFAULT_MAGNET in config.sh)
#   e.g.  sbatch 01_pull_and_make_tsv.sh SUB001 01 3T
#
# What it does
#   1. s3_get_last_dicoms.sh   copies the LAST DICOM of every series (a tiny "mini" copy of the
#                              session) from s3://<bucket>/dicoms/<SUB>_<SES>/
#   2. convert_helper.py       dcm2niix on those -> one small nii.gz + json per series
#   3. nii_init_gpt5.py        reads those jsons -> summaries/sub-<SUB>_ses-<SES>.tsv
#                              (one row per series, with an automatic BIDS label guess)
#   4. apply_label_rules.py    fills labels our protocol names need (helpers/label_rules.csv)
#   5. view_tsv.py             writes summaries/sub-<SUB>_ses-<SES>.view.txt: a short aligned table + CHECKS
#                              (unlabelled series, rest runs missing mag/phase, fmaps without PEdir)
#
# After it finishes: OPEN THE TSV and check the `label` column (rest / fmap / t1w ...).
# Anything with an empty label will NOT be converted. Edit the TSV directly if a label is wrong.
set -Eeuo pipefail

CODE_DIR="${CODE_DIR:-${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}}"
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"

[[ $# -ge 2 ]] || die "Usage: sbatch $(basename "$0") <SUB> <SES> [MAGNET]"
SUB="$1"; SES="$2"; set_magnet "${3:-${DEFAULT_MAGNET}}"
TAG="sub-${SUB}_ses-${SES}"
TSV="${SUMMARY_DIR}/${TAG}.tsv"
S3_DIR="s3://${S3_BUCKET}/dicoms/${SUB}_${SES}/"

load_env
need_cmd python dcm2niix fslinfo

log "1/5 pulling last DICOM of each series: ${S3_DIR}"
mkdir -p "${DICOM_LAST_DIR}/${TAG}"
"${S3_LAST_DICOMS}" -i "${S3_DIR}" -o "${DICOM_LAST_DIR}/${TAG}"

log "2/5 dcm2niix on the mini DICOMs"
# convert_helper.py writes to <-b>/tmp_dcm2bids/sub-<SUB>_ses-<SES>/  (we keep that OUT of the real bids folder)
python "${CDNIPROC}/tools/convert_helper.py" -d "${DICOM_LAST_DIR}/${TAG}" -p "${SUB}" -s "${SES}" -b "${HELPER_DIR}"
HELPER_OUT="${HELPER_DIR}/tmp_dcm2bids/${TAG}"
[[ -d "${HELPER_OUT}" ]] || die "expected ${HELPER_OUT} - convert_helper.py did not create it (is the session label 2 digits, e.g. 01?)"

# keep the previous TSV / view if this session was run before (the TSV is rebuilt from scratch)
for f in "${TSV}" "${TSV%.tsv}.view.txt"; do [[ -f "$f" ]] && cp -p "$f" "$f.prev"; done
log "3/5 building summary TSV"
python "${CDNIPROC}/nii_init_gpt5.py" "${HELPER_OUT}" -p "${SUB}" -s "${SES}" -o "${TSV}"

log "4/5 applying study label rules"
python "${HELPERS}/apply_label_rules.py" "${TSV}" "${HELPERS}/label_rules.csv" "${MAGNET}"

log "5/5 making the text view of the TSV (an alternative to LibreOffice)"
python "${HELPERS}/view_tsv.py" make "${TSV}" --force --print

echo
log "DONE. Review this file, then run step 02:"
echo "    ${TSV}"
echo "    easy-to-read view (open in VS Code / less):  ${TSV%.tsv}.view.txt"
echo "    (columns to check: label, MP (M=magnitude P=phase), PEdir, EchoNumber, SeriesDescription)"
