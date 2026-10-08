#!/bin/bash
# run_subject.sh - submit the whole pipeline for ONE subject as a chain of SLURM jobs.
# Run it on the login node (it only submits jobs; it is not itself an sbatch script).
#
# Usage:
#   ./run_subject.sh <SUB> [MAGNET] <SES> [SES ...] [--from N] [--to N] [--layout combined|sessions] [--use-txt]
#
# Arguments
#   SUB     subject ID without "sub-"                                    e.g. SUB001
#   MAGNET  3T or 7T. Optional: if left out, DEFAULT_MAGNET from config.sh is used (3T unless you change it)
#   SES     one or more session labels without "ses-", two digits as in the S3 folder name   e.g. 01 02
#   --from N / --to N   run only steps N..M of the 5 (default 1..5)
#
# Examples  (subject SUB001, 3T scanner, sessions 01 and 02):
#   ./run_subject.sh SUB001 3T 01 02             # run all 5 steps for both sessions
#   ./run_subject.sh SUB001 01 02                # same thing; MAGNET left out, so DEFAULT_MAGNET (3T) is used
#   ./run_subject.sh SUB001 3T 01 02 --to 1      # step 1 only: make the summary TSVs so you can review them
#   ./run_subject.sh SUB001 3T 01 02 --from 2    # TSVs are reviewed: steps 2-5 (convert, NORDIC, layouts, fMRIPrep + XCP-D)
#   ./run_subject.sh SUB001 3T 01 02 --from 2 --use-txt   # same, but first copy your edits from the .view.txt into the TSV
#   ./run_subject.sh SUB001 7T 7T1 --from 4      # 7T subject, session label 7T1: rerun just step 4 (NORDIC already done)
#
# Chain:  01 -> 02 -> 03   (one chain per session, sessions run in parallel)
#         all 03s -> 04  (waits for NORDIC itself)  -> 05
# Each job starts only if the one before finished OK (--dependency=afterok).
set -Eeuo pipefail

usage() { awk 'NR>1 && /^set -E/{exit} NR>1{print}' "$0"; }
CODE_DIR="$(cd "$(dirname "$0")" && pwd)"; export CODE_DIR
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"

FROM=1; TO=5; LAYOUT=combined; POS=()
while (( $# )); do
    case "$1" in
        --from)   FROM="$2"; shift 2 ;;
        --to)     TO="$2"; shift 2 ;;
        --layout) LAYOUT="$2"; shift 2 ;;
        --use-txt|--use-view) export USE_TXT_EDITS=1; shift ;;   # --use-view = old name, still accepted
        -h|--help) usage; exit 0 ;;
        *) POS+=("$1"); shift ;;
    esac
done
(( ${#POS[@]} >= 2 )) || { usage; exit 1; }
SUB="${POS[0]}"
if is_magnet "${POS[1]}"; then MAG="${POS[1]}"; SESSIONS=("${POS[@]:2}"); else MAG="${DEFAULT_MAGNET}"; SESSIONS=("${POS[@]:1}"); fi
(( ${#SESSIONS[@]} )) || { usage; exit 1; }
for v in SLURM_ACCOUNT S3_BUCKET OUT_BASE; do
    [[ "${!v}" == *YOUR_* ]] && die "${v} in config.sh is still a placeholder (${!v}) - edit config.sh first"
done
set_magnet "${MAG}"            # validates MAGNET, creates folders
(( FROM >= 1 && TO <= 5 && FROM <= TO )) || die "--from/--to must satisfy 1 <= from <= to <= 5"

cd "${CODE_DIR}"               # jobs write logs to ./logs
mkdir -p logs
# absolute log paths: the relative "logs/..." in the #SBATCH lines only works if sbatch is run from this folder
SB=(sbatch --parsable --export=ALL,CODE_DIR="${CODE_DIR}",USE_TXT_EDITS="${USE_TXT_EDITS}" -A "${SLURM_ACCOUNT}"
    -o "${CODE_DIR}/logs/%x_%j.out" -e "${CODE_DIR}/logs/%x_%j.err")

submit() {                     # submit <dependency-or-empty> <script> <args...>  -> echoes job id
    local dep="$1"; shift
    local args=("${SB[@]}")
    # resources for this step come from config.sh (RES_<step>_CPUS/MEM/TIME/PART); they override the #SBATCH header
    local n="${1:0:2}" v name
    for v in CPUS MEM TIME PART; do name="RES_${n}_${v}"; [[ -n "${!name:-}" ]] || die "${name} is not set in config.sh"; done
    local c="RES_${n}_CPUS" m="RES_${n}_MEM" t="RES_${n}_TIME" p="RES_${n}_PART"
    args+=(-c "${!c}" --mem="${!m}" -t "${!t}" -p "${!p}")
    [[ -n "${dep}" ]] && args+=(--dependency="afterok:${dep}")
    local id; id="$("${args[@]}" "$@")"
    echo "${id%%;*}"
}

LAST_PER_SES=()                # the last job of each session's chain
for ses in "${SESSIONS[@]}"; do
    prev=""
    for step in 1 2 3; do
        (( step < FROM || step > TO )) && continue
        case ${step} in
            1) script=01_pull_and_make_tsv.sh ;;
            2) script=02_convert_to_bids.sh ;;
            3) script=03_run_nordic.sh ;;
        esac
        prev="$(submit "${prev}" "${script}" "${SUB}" "${ses}" "${MAG}")"
        echo "sub-${SUB} ses-${ses}  step ${step} (${script})  job ${prev}"
    done
    [[ -n "${prev}" ]] && LAST_PER_SES+=("${prev}")
done

dep="$(IFS=:; echo "${LAST_PER_SES[*]:-}")"
if (( FROM <= 4 && TO >= 4 )); then
    j4="$(submit "${dep}" 04_clean_and_layout.sh "${SUB}" "${MAG}" "${SESSIONS[@]}")"
    echo "sub-${SUB}  step 4 (04_clean_and_layout.sh)  job ${j4}"
    dep="${j4}"
elif (( FROM > 4 )); then
    dep=""
fi
if (( FROM <= 5 && TO >= 5 )); then
    j5="$(submit "${dep}" 05_fmriprep_xcpd.sh "${SUB}" "${MAG}" "${LAYOUT}")"
    echo "sub-${SUB}  step 5 (05_fmriprep_xcpd.sh)  job ${j5}"
fi

echo
echo "Watch:  squeue -u \$USER        Logs: ${CODE_DIR}/logs/"
(( TO == 1 )) && echo "Next: review ${SUMMARY_DIR}/sub-${SUB}_ses-*.tsv, then rerun with --from 2"
exit 0
