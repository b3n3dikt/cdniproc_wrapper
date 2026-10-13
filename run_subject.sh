#!/bin/bash
# run_subject.sh - submit the whole pipeline for ONE subject as a chain of SLURM jobs.
# Run it on the login node (it only submits jobs; it is not itself an sbatch script).
#
# Usage:
#   ./run_subject.sh <SUB> [MAGNET] <SES> [SES ...] [--from N] [--to N] [--layout auto|nordic|combined|sessions] [--use-txt]
#                    [--anat-ref unbiased|first-lex|sessionwise] [--coreg-level subject|session|run]
#                    [--sessions-anat subject|session] [--both-anat] [--tag LABEL]
#                    [--intendedfor-mode auto|lookforward|lookback|nearest] [--qc-on-fail stop|warn]
#
# Arguments
#   SUB     subject ID without "sub-"                                    e.g. SUB001
#   MAGNET  3T or 7T. Optional: if left out, DEFAULT_MAGNET from config.sh is used; if that is empty too (default), no field strength is used
#   SES     one or more session labels without "ses-", two digits as in the S3 folder name   e.g. 01 02
#   --from N / --to N   run only steps N..M of the 5 (default 1..5)
#   --anat-ref X        fMRIPrep --subject-anatomical-reference (default: ANAT_REFERENCE in config.sh = unbiased)
#   --coreg-level X     fMRIPrep --bold-coreg-level (default: COREG_LEVEL in config.sh = subject; session or run to override)
#   --sessions-anat X   bids_sessions layout: anat outside the sessions (subject, default) or inside each session (session)
#   --both-anat         step 04: build BOTH bids_sessions variants (anat outside and anat inside the sessions)
#   --intendedfor-mode  how runs are matched to fieldmaps in step 04 (default: INTENDEDFOR_MODE in config.sh = auto)
#   --qc-on-fail X      stop (default): a QC FAIL in step 04 stops the chain; warn: report only
#   --tag LABEL         suffix for the step-05 output folders, so test runs on the same subject do not overwrite each other
#
# Examples  (subject SUB001, sessions 01 and 02):
#   ./run_subject.sh SUB001 3T 01 02             # run all 5 steps for both sessions
#   ./run_subject.sh SUB001 01 02                # MAGNET left out: runs with no 3T/7T (unless DEFAULT_MAGNET is set in config.sh)
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

FROM=1; TO=5; LAYOUT=auto; POS=()
while (( $# )); do
    case "$1" in
        --from)   FROM="$2"; shift 2 ;;
        --to)     TO="$2"; shift 2 ;;
        --layout) LAYOUT="$2"; shift 2 ;;
        --anat-ref)      ANAT_REFERENCE="$2"; shift 2 ;;
        --coreg-level)   COREG_LEVEL="$2"; shift 2 ;;
        --sessions-anat) SESSIONS_ANAT="$2"; shift 2 ;;
        --tag)           RUN_TAG="$2"; shift 2 ;;
        --both-anat)     export BOTH_ANAT=1; shift ;;
        --intendedfor-mode) INTENDEDFOR_MODE="$2"; shift 2 ;;
        --qc-on-fail)    QC_ON_FAIL="$2"; shift 2 ;;
        --use-txt|--use-view) export USE_TXT_EDITS=1; shift ;;   # --use-view = old name, still accepted
        -h|--help) usage; exit 0 ;;
        *) POS+=("$1"); shift ;;
    esac
done
(( ${#POS[@]} >= 2 )) || { usage; exit 1; }
case "${ANAT_REFERENCE}"  in unbiased|first-lex|sessionwise) ;; *) die "--anat-ref must be unbiased, first-lex or sessionwise (got '${ANAT_REFERENCE}')" ;; esac
case "${COREG_LEVEL}"     in subject|session|run) ;;           *) die "--coreg-level must be subject, session or run (got '${COREG_LEVEL}')" ;; esac
case "${SESSIONS_ANAT}"   in subject|session) ;;               *) die "--sessions-anat must be subject or session (got '${SESSIONS_ANAT}')" ;; esac
[[ -z "${RUN_TAG}" || "${RUN_TAG}" =~ ^[A-Za-z0-9._-]+$ ]] || die "--tag may only contain letters, digits, . _ -"
case "${INTENDEDFOR_MODE}" in auto|nearest|lookback|lookforward) ;; *) die "--intendedfor-mode must be auto, nearest, lookback or lookforward (got '${INTENDEDFOR_MODE}')" ;; esac
case "${QC_ON_FAIL}"       in stop|warn) ;; *) die "--qc-on-fail must be stop or warn (got '${QC_ON_FAIL}')" ;; esac
export ANAT_REFERENCE COREG_LEVEL SESSIONS_ANAT RUN_TAG INTENDEDFOR_MODE QC_ON_FAIL   # picked up by the jobs (sbatch --export=ALL)
SUB="${POS[0]}"
if is_magnet "${POS[1]}"; then MAG="${POS[1]}"; SESSIONS=("${POS[@]:2}"); else MAG="${DEFAULT_MAGNET}"; SESSIONS=("${POS[@]:1}"); fi
(( ${#SESSIONS[@]} )) || { usage; exit 1; }
for v in SLURM_ACCOUNT S3_BUCKET OUT_BASE; do
    [[ "${!v}" == *YOUR_* ]] && die "${v} in config.sh is still a placeholder (${!v}) - edit config.sh first"
done
set_magnet "${MAG}"            # validates MAGNET, creates folders
MAGARG=(); [[ -n "${MAG}" ]] && MAGARG=("${MAG}")   # empty MAGNET: pass nothing (an empty word would be read as a session)
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
    [[ -n "${dep}" ]] && args+=(--dependency="afterok:${dep}" --kill-on-invalid-dep=yes)
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
        prev="$(submit "${prev}" "${script}" "${SUB}" "${ses}" ${MAGARG[@]+"${MAGARG[@]}"})"
        echo "sub-${SUB} ses-${ses}  step ${step} (${script})  job ${prev}"
    done
    [[ -n "${prev}" ]] && LAST_PER_SES+=("${prev}")
done

dep="$(IFS=:; echo "${LAST_PER_SES[*]:-}")"
if (( FROM <= 4 && TO >= 4 )); then
    j4="$(submit "${dep}" 04_clean_and_layout.sh "${SUB}" ${MAGARG[@]+"${MAGARG[@]}"} "${SESSIONS[@]}")"
    echo "sub-${SUB}  step 4 (04_clean_and_layout.sh)  job ${j4}"
    dep="${j4}"
elif (( FROM > 4 )); then
    dep=""
fi
if (( FROM <= 5 && TO >= 5 )); then
    j5="$(submit "${dep}" 05_fmriprep_xcpd.sh "${SUB}" ${MAGARG[@]+"${MAGARG[@]}"} "${LAYOUT}")"
    echo "sub-${SUB}  step 5 (05_fmriprep_xcpd.sh)  job ${j5}"
fi

echo
echo "Watch:  squeue -u \$USER        Logs: ${CODE_DIR}/logs/"
(( TO == 1 )) && echo "Next: review ${SUMMARY_DIR}/sub-${SUB}_ses-*.tsv, then rerun with --from 2"
exit 0
