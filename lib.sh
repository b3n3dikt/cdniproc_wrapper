#!/bin/bash
# lib.sh - small shared functions. Sourced by every step (after config.sh).

log()  { echo "[$(date +%H:%M:%S)] $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# s3_dicom_dir <SUB> <SES>  ->  s3://<bucket>/<S3_DICOM_PATH with {SUB} {SES} {MAGNET} filled in>/
# (call after set_magnet, so {MAGNET} is known)
s3_dicom_dir() {
    local p="${S3_DICOM_PATH}"
    p="${p//\{SUB\}/$1}"; p="${p//\{SES\}/$2}"; p="${p//\{MAGNET\}/${MAGNET}}"
    p="${p#/}"; [[ "${p}" == */ ]] || p="${p}/"
    echo "s3://${S3_BUCKET}/${p}"
}

# is_magnet <word>  ->  true if the word is 3T or 7T (lets MAGNET be left out of a command)
is_magnet() { [[ "${1:-}" =~ ^[37][Tt]$ ]]; }

# set_magnet <3T|7T>  ->  defines ROOT and all the folders below it
set_magnet() {
    case "${1:-}" in
        3T|3t) MAGNET=3T ;;
        7T|7t) MAGNET=7T ;;
        *) die "MAGNET must be 3T or 7T (got '${1:-}')" ;;
    esac
    for bad in bids sub- ses-; do
        [[ "${OUT_BASE}" == *"${bad}"* ]] && die "OUT_BASE contains '${bad}' - the lab NORDIC scripts split paths on that word. Change OUT_BASE in config.sh."
    done
    if [[ "${USE_MAGNET_FOLDER:-0}" == "1" ]]; then ROOT="${OUT_BASE%/}/${MAGNET}"   # OUT_BASE/3T/...
    else
        ROOT="${OUT_BASE%/}"                                                          # OUT_BASE/... (no 3T/7T folder)
        [[ -d "${ROOT}/${MAGNET}/bids" ]] && echo "WARNING: ${ROOT}/${MAGNET}/bids exists but USE_MAGNET_FOLDER is not 1, so outputs go to ${ROOT}/ . If your earlier data is in ${MAGNET}/, set USE_MAGNET_FOLDER=1 in config.sh." >&2
    fi
    DICOM_LAST_DIR="${ROOT}/dicoms_last"      # one DICOM per series (for the summary TSV)
    DICOM_DIR="${ROOT}/dicoms"                # full DICOMs
    HELPER_DIR="${ROOT}/helper"               # dcm2niix output used to make the TSV
    SUMMARY_DIR="${ROOT}/summaries"           # <sub>_<ses>.tsv / .json, nordic command files, file maps
    RAW_BIDS="${ROOT}/bids"                   # dcm2bids output + NORDIC outputs (written in place)
    NORDIC_DIR="${ROOT}/derivatives/nordic"   # NORDIC-cleaned, per session
    BIDS_COMBINED="${ROOT}/bids_combined"     # sub-X/ses-combined/...   (fMRIPrep input)
    BIDS_SESSIONS="${ROOT}/bids_sessions"     # sub-X/anat + sub-X/ses-N/...
    JOB_DIR="${ROOT}/jobs"                    # job IDs of child jobs each step waits for
    LOG_DIR="${ROOT}/logs"
    export MAGNET ROOT DICOM_LAST_DIR DICOM_DIR HELPER_DIR SUMMARY_DIR RAW_BIDS NORDIC_DIR \
           BIDS_COMBINED BIDS_SESSIONS JOB_DIR LOG_DIR
    mkdir -p "${SUMMARY_DIR}" "${JOB_DIR}" "${LOG_DIR}" "${ROOT}/logs/nordic"
}

load_env() {          # conda env + modules that every python step needs
    set +u
    source "${CONDA_LOADER}"
    conda activate "${CONDA_ENV}"
    module load "${FSL_MODULE}" 2>/dev/null || log "WARNING: could not load ${FSL_MODULE}"
    set -u
}

need_cmd() { for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "'$c' not found on PATH (conda env ${CONDA_ENV} / modules loaded?)"; done; }

# ---- child-job bookkeeping --------------------------------------------------
# A step that submits its own sbatch jobs (NORDIC, bias-field) records the IDs so
# step 04 can wait for them. File: $JOB_DIR/sub-<SUB>_ses-<SES>.jobids
jobfile() { echo "${JOB_DIR}/sub-$1_ses-$2.jobids"; }

submit_and_record() {     # submit_and_record <jobfile> <sbatch args...>
    local jf="$1"; shift
    local out id
    out="$(sbatch --parsable "$@")" || die "sbatch failed: sbatch $*"
    id="${out%%;*}"
    echo "${id}" >> "${jf}"
    log "submitted job ${id}"
}

# wait_for_jobs <jobfile...> : poll squeue until every recorded job has left the queue
wait_for_jobs() {
    local ids=() f id
    for f in "$@"; do
        [[ -f "$f" ]] || continue
        while IFS= read -r id; do [[ -n "$id" ]] && ids+=("$id"); done < "$f"
    done
    (( ${#ids[@]} )) || { log "no child jobs to wait for"; return 0; }
    local csv; csv="$(IFS=,; echo "${ids[*]}")"
    log "waiting for ${#ids[@]} child job(s): ${csv}"
    while [[ -n "$(squeue -h -j "${csv}" -o %i 2>/dev/null)" ]]; do sleep "${WAIT_SECONDS:-60}"; done
    log "child jobs finished. Final states:"
    sacct -j "${csv}" -X -n -o JobID,JobName%20,State,Elapsed 2>/dev/null || true
}

# ---- IntendedFor --------------------------------------------------------------
# run_intendedfor <bids_root> <SUB> <SES> [jsbr-assign-mode]
run_intendedfor() {
    local root="$1" sub="$2" ses="$3" mode="${4:-auto}" method="${INTENDEDFOR_METHOD}"
    [[ "${5:-}" == "force-jsbr" ]] && method=jsbr
    log "IntendedFor (${method}) on ${root} sub-${sub} ses-${ses}"
    case "${method}" in
        jsbr) python "${HELPERS}/IntendedFor_JSBR.py" "${root}" --sub "sub-${sub}" --ses "ses-${ses}" \
                     --assign-mode "${mode}" --write ;;
        lab)  mkdir -p "${LOG_DIR}/intendedfor"
              ( cd "${LOG_DIR}/intendedfor" && python "${CDNIPROC}/tools/IntendedFor_new.py" "${root}/sub-${sub}/ses-${ses}" ) ;;
        *)    die "INTENDEDFOR_METHOD must be jsbr or lab" ;;
    esac
}

# sessions_of <bids_root> <SUB>  -> prints session labels (no 'ses-') one per line
sessions_of() { local d; for d in "$1"/sub-"$2"/ses-*; do [[ -d "$d" ]] && basename "$d" | sed 's/^ses-//'; done | sort; }
