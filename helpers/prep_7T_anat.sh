#!/bin/bash
# prep_7T_anat.sh <SUB> <SES>      (called by 03_run_nordic.sh for 7T only)
#
# 7T T1w comes from an MP2RAGE: INV1 + INV2 + UNI. This
#   1. denoises the UNI image with LayNii (LN_MP2RAGE_DNOISE, beta 0.5)      -> acq-unidenoised
#   2. submits a bias-field-correction job (SPM, via MATLAB) for it           -> acq-unidenoisedbfc
#   3. submits a bias-field-correction job for the T2w, if there is one       -> acq-bfc
# Outputs are written next to the originals in bids/sub-X/ses-Y/anat. Step 04
# (helpers/cleanup_7T_anat.sh) later picks the *bfc files and renames them ..._run-NN_T1w / _T2w.
# Bias-field job IDs are added to the session's job file so step 04 waits for them.
#
# Input names: the current cdniproc_v2.0 naming (..._inv-1_..._MP2RAGE, ..._inv-2_..._MP2RAGE,
# ..._UNIT1) and, as a fallback, the old heuristic naming (..._acq-inv1_T1w etc.).
set -Eeuo pipefail
shopt -s nullglob

SUB="$1"; SES="$2"
CODE_DIR="${CODE_DIR:-${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}}"
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"
set_magnet 7T
JF="$(jobfile "${SUB}" "${SES}")"
ANAT="${RAW_BIDS}/sub-${SUB}/ses-${SES}/anat"
PFX="sub-${SUB}_ses-${SES}"

[[ -f "${HELPERS}/Bias_field_script_job.m" ]] || die "helpers/Bias_field_script_job.m is missing (copy it from the old pCodePath folder on the server)"
[[ -x "${LAYNII_DIR}/LN_MP2RAGE_DNOISE" ]] || die "LN_MP2RAGE_DNOISE not found/executable in ${LAYNII_DIR} (LAYNII_DIR in config.sh)"

set +u; module load gcc/9.2.0; module load afni; set -u

first() { local f; for f in "$@"; do echo "$f"; return; done; }   # first glob match or nothing
INV1=$(first "${ANAT}/${PFX}"*_inv-1_*MP2RAGE.nii.gz "${ANAT}/${PFX}"_acq-inv1*_T1w.nii.gz)
INV2=$(first "${ANAT}/${PFX}"*_inv-2_*MP2RAGE.nii.gz "${ANAT}/${PFX}"_acq-inv2*_T1w.nii.gz)
UNI=$(first  "${ANAT}/${PFX}"*_UNIT1.nii.gz          "${ANAT}/${PFX}"_acq-uni*_T1w.nii.gz)

if [[ -z "${INV1}" || -z "${INV2}" || -z "${UNI}" ]]; then
    echo "WARNING: could not find INV1/INV2/UNI in ${ANAT} - skipping MP2RAGE denoise/BFC."
    echo "         Found: INV1='${INV1}' INV2='${INV2}' UNI='${UNI}'"
    echo "         Check the label / inv columns of the session TSV (step 01)."
else
    RUN=$(grep -oE '_run-[0-9]+' <<<"$(basename "${UNI}")" | head -n1 || true)
    RUN="${RUN:-_run-01}"
    DENOISED="${ANAT}/${PFX}_acq-unidenoised${RUN}_T1w.nii.gz"
    FINAL="${ANAT}/${PFX}_acq-unidenoisedbfc${RUN}_T1w.nii.gz"

    log "LayNii MP2RAGE denoise"
    ( cd "${LAYNII_DIR}" && ./LN_MP2RAGE_DNOISE -INV1 "${INV1}" -INV2 "${INV2}" -UNI "${UNI}" -beta 0.5 -output "${DENOISED}" )

    log "bias-field correction job (T1w)"
    submit_and_record "${JF}" -A "${SLURM_ACCOUNT}" "${HELPERS}/start_bias_field_correction.sh" "${DENOISED}" "${FINAL}" "${HELPERS}"
    cp "${UNI%.nii.gz}.json" "${FINAL%.nii.gz}.json"
fi

T2W=""
for f in "${ANAT}/${PFX}"*_T2w.nii.gz; do          # skip outputs of an earlier run of this script
    [[ "$f" == *_acq-bfc* ]] || { T2W="$f"; break; }
done
if [[ -n "${T2W}" ]]; then
    RUN=$(grep -oE '_run-[0-9]+' <<<"$(basename "${T2W}")" | head -n1 || true)
    RUN="${RUN:-_run-01}"
    T2FINAL="${ANAT}/${PFX}_acq-bfc${RUN}_T2w.nii.gz"
    log "bias-field correction job (T2w)"
    submit_and_record "${JF}" -A "${SLURM_ACCOUNT}" "${HELPERS}/start_bias_field_correction.sh" "${T2W}" "${T2FINAL}" "${HELPERS}"
    cp "${T2W%.nii.gz}.json" "${T2FINAL%.nii.gz}.json"
else
    log "no T2w found (fine if this session has none)"
fi
