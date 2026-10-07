#!/bin/bash -l
#SBATCH -J 04_layout
#SBATCH -c 4
#SBATCH --mem=32G
#SBATCH -t 12:00:00
#SBATCH -p ag2tb,agsmall,aglarge
#SBATCH -o logs/%x_%j.out
#SBATCH -e logs/%x_%j.err
#
# STEP 4 - per SUBJECT (after every session finished steps 02 + 03):
#          check NORDIC, finish 7T extras, build the two final BIDS datasets, set IntendedFor.
#
# Usage:  sbatch 04_clean_and_layout.sh <SUB> [MAGNET] [SES ...]      (MAGNET omitted = DEFAULT_MAGNET in config.sh)
#         (no SES given = every session found in <ROOT>/bids/sub-<SUB>)
#
# What it does
#   a. waits for the NORDIC / bias-field jobs started by step 03
#   b. post-NORDIC check + move:  postnordic.py (lab v2.0) verifies every mag run has a NORDIC run,
#      moves the NORDIC files to derivatives/nordic/sub-X/ses-Y/func and copies anat/fmap there.
#      >> stops here with an error if any session's NORDIC failed <<
#   c. 7T only:  final T1w/T2w are copied in (cleanup_7T_anat.sh); a provisional IntendedFor is written,
#      then a fake forward-PE (AP) fieldmap is made from the functional data (7T fmaps are PA only)
#   d. make_layouts.py builds   bids_combined/  and  bids_sessions/   from derivatives/nordic
#   e. IntendedFor is written again in BOTH new datasets (file names/paths changed in step d)
set -Eeuo pipefail

CODE_DIR="${CODE_DIR:-${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}}"
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"

[[ $# -ge 1 ]] || die "Usage: sbatch $(basename "$0") <SUB> [MAGNET] [SES ...]"
SUB="$1"; shift
if is_magnet "${1:-}"; then set_magnet "$1"; shift; else set_magnet "${DEFAULT_MAGNET}"; fi
SESSIONS=("$@")
if (( ${#SESSIONS[@]} == 0 )); then
    while IFS= read -r s; do SESSIONS+=("$s"); done < <(sessions_of "${RAW_BIDS}" "${SUB}")
fi
(( ${#SESSIONS[@]} )) || die "no sessions found in ${RAW_BIDS}/sub-${SUB}"
log "sub-${SUB} (${MAGNET}) sessions: ${SESSIONS[*]}"

load_env
need_cmd python

# ---- a. wait for child jobs ---------------------------------------------------
JFS=(); for s in "${SESSIONS[@]}"; do JFS+=("$(jobfile "${SUB}" "${s}")"); done
wait_for_jobs "${JFS[@]}"

# ---- b. post-NORDIC check + move ----------------------------------------------
FAILED=()
for s in "${SESSIONS[@]}"; do
    log "post-NORDIC: ses-${s}"
    # anat/fmap/... are COPIED to derivatives/nordic only if missing -> clear old copies so they refresh
    if [[ -d "${NORDIC_DIR}/sub-${SUB}/ses-${s}" ]]; then
        find "${NORDIC_DIR}/sub-${SUB}/ses-${s}" -mindepth 1 -maxdepth 1 -type d ! -name func -exec rm -rf {} +
    fi
    python "${CDNIPROC}/tools/postnordic.py" "${RAW_BIDS}/sub-${SUB}/ses-${s}" | tee "${LOG_DIR}/postnordic_sub-${SUB}_ses-${s}.txt"
    if grep -q "NORDIC FAILED" "${LOG_DIR}/postnordic_sub-${SUB}_ses-${s}.txt"; then FAILED+=("${s}"); fi
done
if (( ${#FAILED[@]} )); then
    die "NORDIC FAILED for session(s): ${FAILED[*]}. Look at ${ROOT}/logs/nordic/*.err, fix, rerun step 03 for those sessions, then rerun step 04."
fi

# stray fmaps with an empty 'dir-' come from a missing PhaseEncodingDirection; fMRIPrep cannot use them
for f in "${NORDIC_DIR}/sub-${SUB}"/ses-*/fmap/*dir-_*; do
    [[ -e "$f" ]] && { log "removing malformed fmap file: $f"; rm -f "$f"; }
done

# ---- c. 7T extras ----------------------------------------------------------------
if [[ "${MAGNET}" == "7T" ]]; then
    module load "${FSL_MODULE}" 2>/dev/null || true       # generate_fake_ap.py uses fslroi
    for s in "${SESSIONS[@]}"; do
        log "7T: final anatomicals, provisional IntendedFor, fake AP: ses-${s}"
        bash "${HELPERS}/cleanup_7T_anat.sh" "${SUB}" "${s}" --src-base "${RAW_BIDS}" --dst-base "${NORDIC_DIR}"
        run_intendedfor "${NORDIC_DIR}" "${SUB}" "${s}" lookback force-jsbr
        python "${HELPERS}/generate_fake_ap.py" "${NORDIC_DIR}" "${SUB}" "${s}" -v
    done
fi

# ---- d. build the two datasets ----------------------------------------------------
log "building bids_combined and bids_sessions"
ANAT_FLAG=(); [[ "${SESSIONS_ANAT}" == "session" ]] && ANAT_FLAG=(--anat-in-session)
python "${HELPERS}/make_layouts.py" \
    --nordic-root "${NORDIC_DIR}" --sub "${SUB}" --sessions "${SESSIONS[@]}" \
    --out-combined "${BIDS_COMBINED}" --out-sessions "${BIDS_SESSIONS}" \
    --dataset-description "${HELPERS}/dataset_description.json" \
    --map-out "${SUMMARY_DIR}/layout_map_sub-${SUB}.tsv" "${ANAT_FLAG[@]}"

# ---- e. IntendedFor on the final datasets -------------------------------------------
run_intendedfor "${BIDS_COMBINED}" "${SUB}" combined auto
for s in "${SESSIONS[@]}"; do
    run_intendedfor "${BIDS_SESSIONS}" "${SUB}" "${s}" auto
done

echo
log "DONE. Final datasets:"
echo "    ${BIDS_COMBINED}/sub-${SUB}/ses-combined/{anat,func,fmap}"
echo "    ${BIDS_SESSIONS}/sub-${SUB}/{anat, ses-*/func, ses-*/fmap}"
echo "    old-name -> new-name table: ${SUMMARY_DIR}/layout_map_sub-${SUB}.tsv"
echo "Check: every fmap json has an IntendedFor:"
echo "    grep -L IntendedFor ${BIDS_COMBINED}/sub-${SUB}/ses-combined/fmap/*.json   (should print nothing)"
