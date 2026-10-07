#!/bin/bash -l
#SBATCH -J 05_fmriprep_xcpd
#SBATCH -c 24
#SBATCH --mem=650G
#SBATCH -t 50:00:00
#SBATCH -p ag2tb,agsmall,aglarge,msismall
#SBATCH -o logs/%x_%j.out
#SBATCH -e logs/%x_%j.err
#
# STEP 5 - fMRIPrep, then XCP-D, then the lab's post-XCP-D conversions. Per SUBJECT.
#
# Usage:  sbatch 05_fmriprep_xcpd.sh <SUB> [MAGNET] [combined|sessions]      (MAGNET omitted = DEFAULT_MAGNET in config.sh)
#         default layout = combined  (reads bids_combined, the single ses-combined session)
#         "sessions" runs on bids_sessions (all of the subject's sessions in one fMRIPrep run).
#         NOTE: sessions layout (anat at sub-X/anat) has not been test-run through fMRIPrep yet.
#
# Output (under <ROOT>/derivatives):
#   <cifti>_fmriprep_<ver>/output/<SUB>     fMRIPrep
#   <cifti>_xcpd_<ver>/output/<SUB>         XCP-D  <-- final deliverable (denoised + interpolated CIFTI, QC, motion .tsv)
#
# Versions and flags: FMRIPREP_VERSION / FMRIPREP_FLAGS / XCPD_VERSION / XCPD_FLAGS in config.sh (defaults are the lab's abcd-mode settings).
# Resources: RES_05_* in config.sh (applied by run_subject.sh; the #SBATCH lines below are the defaults for a bare sbatch).
set -Eeuo pipefail

CODE_DIR="${CODE_DIR:-${SLURM_SUBMIT_DIR:-$(cd "$(dirname "$0")" && pwd)}}"
source "${CODE_DIR}/config.sh"; source "${CODE_DIR}/lib.sh"

[[ $# -ge 1 ]] || die "Usage: sbatch $(basename "$0") <SUB> [MAGNET] [combined|sessions]"
SUB="$1"; shift
if is_magnet "${1:-}"; then set_magnet "$1"; shift; else set_magnet "${DEFAULT_MAGNET}"; fi
LAYOUT="${1:-combined}"
case "${LAYOUT}" in
    combined) BIDS_IN="${BIDS_COMBINED}" ;;
    sessions) BIDS_IN="${BIDS_SESSIONS}" ;;
    *) die "layout must be combined or sessions" ;;
esac
[[ -d "${BIDS_IN}/sub-${SUB}" ]] || die "${BIDS_IN}/sub-${SUB} not found - run step 04 first"

DERIV="${ROOT}/derivatives"
FP_OUT="${DERIV}/${CIFTI_SPACE}_fmriprep_${FMRIPREP_VERSION}/output/${SUB}"
FP_WORK="${DERIV}/${CIFTI_SPACE}_fmriprep_${FMRIPREP_VERSION}/work/${SUB}"
XC_OUT="${DERIV}/${CIFTI_SPACE}_xcpd_${XCPD_VERSION}/output/${SUB}"
XC_WORK="${DERIV}/${CIFTI_SPACE}_xcpd_${XCPD_VERSION}/work/${SUB}"
mkdir -p "${FP_OUT}" "${FP_WORK}" "${XC_OUT}" "${XC_WORK}"

[[ -f "${BIDS_IN}/dataset_description.json" ]] || cp -v "${HELPERS}/dataset_description.json" "${BIDS_IN}/"

load_env
set +u; module load singularity 2>/dev/null; module load "${MATLAB_MODULE}"; set -u
SINGULARITY="$(which singularity)"

log "fMRIPrep ${FMRIPREP_VERSION} on ${BIDS_IN}"
env -i "${SINGULARITY}" run --cleanenv \
    -B /tmp:/tmp \
    -B "${BIDS_IN}":/bids_dir \
    -B "${FP_OUT}":/output_dir \
    -B "${FP_WORK}":/wd \
    -B "${FS_LICENSE}":/opt/freesurfer/license.txt \
    "${PIPELINES_DIR}/fmriprep/fmriprep_${FMRIPREP_VERSION}.sif" \
    "${FMRIPREP_FLAGS[@]}" \
    --fs-license-file /opt/freesurfer/license.txt \
    --participant-label "${SUB}" \
    -w /wd \
    /bids_dir /output_dir participant

log "XCP-D ${XCPD_VERSION}"
env -i "${SINGULARITY}" run --cleanenv \
    -B "${FS_LICENSE}":/opt/freesurfer/license.txt \
    -B "${FP_OUT}":/data:ro \
    -B "${XC_OUT}":/out \
    -B "${XC_WORK}":/work \
    "${PIPELINES_DIR}/xcp_d/xcp_d_${XCPD_VERSION}.sif" \
    "${XCPD_FLAGS[@]}" \
    --participant-label "${SUB}" \
    -w /work \
    /data /out participant

log "post-XCP-D conversions (every session folder in the XCP-D output)"
for FUNCDIR in "${XC_OUT}"/sub-"${SUB}"/ses-*/func; do
    [[ -d "${FUNCDIR}" ]] || continue
    log "  ${FUNCDIR}"
    # motion .hdf5 -> .mat
    matlab -nodisplay -nosplash -r "addpath(genpath('${XCPD_MOTION}')); hdf5_files=dir('${FUNCDIR}/*.hdf5'); for i = 1:length(hdf5_files); xcpd2dcanmotion(fullfile(hdf5_files(i).folder, hdf5_files(i).name), hdf5_files(i).folder); end; exit"
    # spatial interpolation of high-motion frames in the denoised CIFTIs
    for FILE in "${FUNCDIR}"/*denoised_bold.dtseries.nii; do
        [[ -e "${FILE}" ]] || continue
        log "  interpolating ${FILE}"
        matlab -nodisplay -nosplash -r "addpath(genpath('${INTERP}')); addpath(genpath('${COMPARE}')); cii_save_name=interpolate_noise_for_timeseries('${FILE}','${WB}',0); exit"
    done
done
# motion .hdf5 -> .tsv (whole XCP-D output) and template-matching input folder
python "${XCPD_MOTION}/convert_motion_hdf5_to_tsv.py" "${XC_OUT}"
python "${CDNIPROC}/tools/copy_files_to_temp.py" "${XC_OUT}"

log "DONE. Final output: ${XC_OUT}"
