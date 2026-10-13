#!/bin/bash
# run_layout_tests.sh - set up and submit the layout / coreg-level comparison (plan in docs: fmriprep_session_flags_guide.pdf, section 6)
# Run it on the login node (like run_subject.sh), after step 03 (NORDIC) has finished for every session.
#
# Usage:
#   ./run_layout_tests.sh layouts  <SUB> [MAGNET] <SES> [SES ...]                      # step 04: builds all three layouts
#   ./run_layout_tests.sh fmriprep <SUB> [MAGNET] <SES> [SES ...] [--only A,D] [--dry-run]   # step 05: one fMRIPrep+XCP-D run per condition
#
# Layouts built by "layouts" (one step-04 job; wait for it to finish before "fmriprep"):
#   L1  bids_combined                     one ses-combined session, all runs renumbered
#   L2  bids_sessions                     sessions kept, anat outside the sessions (sub-X/anat)
#   L3  bids_sessions_anat-in-ses         sessions kept, anat inside every session (standard BIDS)
#
# Conditions submitted by "fmriprep" (always --subject-anatomical-reference unbiased; output folder suffix = the letter):
#   A  L1  --bold-coreg-level subject        D  L1  --bold-coreg-level run
#   B  L2  --bold-coreg-level subject        E  L2  --bold-coreg-level run
#   C  L3  --bold-coreg-level subject        F  L3  --bold-coreg-level run
#   A-rep  L1 subject again (the noise floor; only when asked for with --only A-rep, or --all)
# Each condition gets its own output folders (<ROOT>/derivatives/91k_fmriprep_<ver>_<letter>), so nothing is shared or overwritten.
# By default every condition A..F is submitted; they are independent jobs, so they may run at the same time.
set -Eeuo pipefail
CODE_DIR="$(cd "$(dirname "$0")" && pwd)"; export CODE_DIR
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"

usage() { awk 'NR>1 && /^set -E/{exit} NR>1{print}' "$0"; }
MODE="${1:-}"; [[ "${MODE}" == "layouts" || "${MODE}" == "fmriprep" ]] || { usage; exit 1; }
shift
ONLY=""; DRY=0; ARGS=()
while (( $# )); do
    case "$1" in
        --only)    ONLY="$2"; shift 2 ;;
        --all)     ONLY="A,B,C,D,E,F,A-rep"; shift ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
(( ${#ARGS[@]} >= 2 )) || { usage; exit 1; }
SUB="${ARGS[0]}"
if is_magnet "${ARGS[1]}"; then MAG="${ARGS[1]}"; SES=("${ARGS[@]:2}"); MAGARG=("${MAG}")
else MAG=""; SES=("${ARGS[@]:1}"); MAGARG=(); fi
(( ${#SES[@]} )) || { usage; exit 1; }

run() {   # run <command...>: print it, and run it unless --dry-run
    echo "+ $*"
    (( DRY )) || "$@"
}
SUBMIT=("${CODE_DIR}/run_subject.sh" "${SUB}" ${MAGARG[@]+"${MAGARG[@]}"} "${SES[@]}")

if [[ "${MODE}" == "layouts" ]]; then
    run "${SUBMIT[@]}" --from 4 --to 4 --both-anat
    echo
    echo "Step 04 submitted. When it has finished (squeue -u \$USER), check the three layouts and run:"
    echo "    ./run_layout_tests.sh fmriprep ${SUB} ${MAG:+${MAG} }${SES[*]}"
    exit 0
fi

# ---- fmriprep mode ---------------------------------------------------------------
set_magnet "${MAG}"            # defines ROOT and the layout folders (SESSIONS_ANAT is not used here: both variants are named explicitly)
L1="${BIDS_COMBINED}"; L2="${ROOT}/bids_sessions"; L3="${ROOT}/bids_sessions_anat-in-ses"
for d in "${L1}" "${L2}" "${L3}"; do
    [[ -d "${d}/sub-${SUB}" ]] || die "${d}/sub-${SUB} not found - run './run_layout_tests.sh layouts ${SUB} ${MAG:+${MAG} }${SES[*]}' first and wait for it to finish"
done

cond() {   # cond <letter> -> "<layout> <sessions-anat> <coreg-level>"  (a case, not an associative array, so it also runs on old bash)
    case "$1" in
        A)     echo "combined subject subject" ;;
        B)     echo "sessions subject subject" ;;
        C)     echo "sessions session subject" ;;
        D)     echo "combined subject run" ;;
        E)     echo "sessions subject run" ;;
        F)     echo "sessions session run" ;;
        A-rep) echo "combined subject subject" ;;
        *)     return 1 ;;
    esac
}
CONDITIONS=(A B C D E F)
if [[ -n "${ONLY}" ]]; then IFS=',' read -r -a CONDITIONS <<< "${ONLY}"; fi
for c in "${CONDITIONS[@]}"; do cond "${c}" >/dev/null || die "unknown condition '${c}' (A B C D E F A-rep)"; done

for c in "${CONDITIONS[@]}"; do
    read -r LAYOUT SESANAT COREG <<< "$(cond "${c}")"
    run "${SUBMIT[@]}" --from 5 --to 5 \
        --layout "${LAYOUT}" --sessions-anat "${SESANAT}" \
        --anat-ref unbiased --coreg-level "${COREG}" --tag "${c}"
done
echo
echo "Outputs: ${DERIV_BASE:-${ROOT}/derivatives}/${CIFTI_SPACE}_fmriprep_${FMRIPREP_VERSION}_<A..F>/output/${SUB}"
echo "         ${DERIV_BASE:-${ROOT}/derivatives}/${CIFTI_SPACE}_xcpd_${XCPD_VERSION}_<A..F>/output/${SUB}"
