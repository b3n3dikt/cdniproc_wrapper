#!/usr/bin/env bash
set -Eeuo pipefail

# cleanup_7T_anat.sh
#
# Usage:
#   cleanup_7T_anat.sh <SUB> [<SES>] --src-base </path/to/rawbids> --dst-base </path/to/dst_bids>
#
# Behavior:
#   - If <SES> is omitted, processes ALL ses-* under sub-<SUB>. If none exist but sub-level anat exists, uses that.
#   - Copies final T1w/T2w outputs from:
#       <src-base>/sub-<SUB>[/ses-<SES>]/anat/
#     to:
#       <dst-base>/sub-<SUB>[/ses-<SES>]/anat/
#   - T1w pattern:  *_acq-unidenoisedbfc*_T1w.nii.gz  (+ adjacent .json if present)
#   - T2w pattern:  *_acq-bfc*_T2w.nii.gz             (+ adjacent .json if present)
#   - Cleans previously staged T1w/T2w (nii/json) in the destination before copying.
#
# Examples:
#   cleanup_7T_anat.sh PFM3T7T01 7T1 --src-base /raw/bids --dst-base /proc/7T/bids
#   cleanup_7T_anat.sh PFM3T7T01    --src-base /raw/bids --dst-base /proc/7T/bids

show_usage() {
  sed -n '1,80p' "$0" | sed 's/^# \{0,1\}//'
}

# ---- parse args ----
src_base=""
dst_base=""
positional=()

while (( $# )); do
  case "${1:-}" in
    --src-base)
      src_base="${2:?--src-base requires a path}"; shift 2;;
    --dst-base)
      dst_base="${2:?--dst-base requires a path}"; shift 2;;
    -h|--help)
      show_usage; exit 0;;
    --)
      shift; while (( $# )); do positional+=("$1"); shift; done;;
    --*)
      echo "ERROR: Unknown option: $1" >&2; show_usage; exit 2;;
    *)
      positional+=("$1"); shift;;
  esac
done

if (( ${#positional[@]} < 1 || ${#positional[@]} > 2 )); then
  echo "ERROR: Expect <SUB> [<SES>]." >&2
  show_usage
  exit 2
fi

SUB="${positional[0]}"
SES="${positional[1]:-}"

if [[ -z "$src_base" || -z "$dst_base" ]]; then
  echo "ERROR: --src-base and --dst-base are required." >&2
  show_usage
  exit 2
fi

# ---- helpers ----
normalize_ses() {
  local s="$1"
  s="${s#ses-}"
  printf "ses-%s" "$s"
}

# Extract run suffix, default to _run-01 when missing
get_run_suffix() {
  local f="$1"
  if [[ "$f" =~ _run-[0-9]{2} ]]; then
    grep -oE '_run-[0-9]{2}' <<<"$f" | head -n1
  else
    printf "%s" "_run-01"
  fi
}

# ---- collect sessions ----
sub_dir="${src_base}/sub-${SUB}"
if [[ ! -d "$sub_dir" ]]; then
  echo "ERROR: Subject not found: ${sub_dir}" >&2
  exit 1
fi

declare -a ses_list
if [[ -z "${SES}" ]]; then
  # All sessions with anat, or subject-level anat
  if compgen -G "${sub_dir}/ses-*/anat" > /dev/null; then
    # find all ses-*/anat (depth 2)
    while IFS= read -r -d '' p; do
      ses_list+=( "$(basename "$(dirname "$p")")" )
    done < <(find "${sub_dir}" -maxdepth 2 -type d -name anat -print0)
  elif [[ -d "${sub_dir}/anat" ]]; then
    ses_list+=( "" )  # subject-level
  else
    echo "ERROR: No ses-*/anat and no subject-level anat under ${sub_dir}" >&2
    exit 1
  fi
else
  ses_list+=( "$(normalize_ses "$SES")" )
fi

# ---- process ----
shopt -s nullglob

for ses in "${ses_list[@]}"; do
  if [[ -z "$ses" ]]; then
    src_anat="${sub_dir}/anat"
    dst_anat="${dst_base}/sub-${SUB}/anat"
    ses_tag=""
    ses_disp="(no-session)"
  else
    src_anat="${sub_dir}/${ses}/anat"
    dst_anat="${dst_base}/sub-${SUB}/${ses}/anat"
    ses_tag="_${ses}"
    ses_disp="$ses"
  fi

  if [[ ! -d "$src_anat" ]]; then
    echo "INFO: Skipping ${ses_disp}: source anat not found: ${src_anat}"
    continue
  fi

  echo "=== sub-${SUB} ${ses_disp} ==="
  mkdir -p "${dst_anat}"

  # Clean prior staged T1w/T2w
  rm -f "${dst_anat}/sub-${SUB}${ses_tag}"*_T1w.nii.gz \
        "${dst_anat}/sub-${SUB}${ses_tag}"*_T1w.json \
        "${dst_anat}/sub-${SUB}${ses_tag}"*_T2w.nii.gz \
        "${dst_anat}/sub-${SUB}${ses_tag}"*_T2w.json || true

  # T1w finals: *_acq-unidenoisedbfc*_T1w.nii.gz
  t1_list=( "${src_anat}/sub-${SUB}${ses_tag}"*_acq-unidenoisedbfc*"_T1w.nii.gz" )
  if (( ${#t1_list[@]} )); then
    for t1_src in "${t1_list[@]}"; do
      t1_run="$(get_run_suffix "$t1_src")"
      t1_dst_stem="${dst_anat}/sub-${SUB}${ses_tag}${t1_run}_T1w"
      echo "Copying T1w:"
      echo "  ${t1_src}  ->  ${t1_dst_stem}.nii.gz"
      cp -av -- "${t1_src}" "${t1_dst_stem}.nii.gz"
      t1_json="${t1_src%.nii.gz}.json"
      if [[ -f "$t1_json" ]]; then
        echo "  ${t1_json} -> ${t1_dst_stem}.json"
        cp -av -- "${t1_json}" "${t1_dst_stem}.json"
      else
        echo "  (no T1w JSON found next to ${t1_src})"
      fi
    done
  else
    echo "WARN: No final T1w found matching *_acq-unidenoisedbfc*_T1w.nii.gz in ${src_anat}"
  fi

  # T2w finals: *_acq-bfc*_T2w.nii.gz
  t2_list=( "${src_anat}/sub-${SUB}${ses_tag}"*_acq-bfc*"_T2w.nii.gz" )
  if (( ${#t2_list[@]} )); then
    for t2_src in "${t2_list[@]}"; do
      t2_run="$(get_run_suffix "$t2_src")"
      t2_dst_stem="${dst_anat}/sub-${SUB}${ses_tag}${t2_run}_T2w"
      echo "Copying T2w:"
      echo "  ${t2_src}  ->  ${t2_dst_stem}.nii.gz"
      cp -av -- "${t2_src}" "${t2_dst_stem}.nii.gz"
      t2_json="${t2_src%.nii.gz}.json"
      if [[ -f "$t2_json" ]]; then
        echo "  ${t2_json} -> ${t2_dst_stem}.json"
        cp -av -- "${t2_json}" "${t2_dst_stem}.json"
      else
        echo "  (no T2w JSON found next to ${t2_src})"
      fi
    done
  else
    echo "INFO: No final T2w found matching *_acq-bfc*_T2w.nii.gz in ${src_anat}"
  fi
done

echo "Done."

# #!/usr/bin/env bash
# set -Eeuo pipefail

# # Usage:
# #   cleanup_7T_anat.sh <rawbids_dir> <SUB> <SES> <PARENT>
# #
# # Example:
# #   cleanup_7T_anat.sh /scratch/.../bids PFM3T7T01 7T1 /scratch/.../processing/7T

# rawbids_dir=${1:?rawbids_dir required}
# SUB=${2:?SUB required}
# SES=${3:?SES required}
# PARENT=${4:?PARENT required}

# src_anat="${rawbids_dir}/sub-${SUB}/ses-${SES}/anat"
# dst_anat="${PARENT}/bids/sub-${SUB}/ses-${SES}/anat"

# mkdir -p "${dst_anat}"

# # Clean prior staged T1w/T2w (only those leave any other files)
# rm -f "${dst_anat}/sub-${SUB}_ses-${SES}"*_T1w.nii.gz \
#       "${dst_anat}/sub-${SUB}_ses-${SES}"*_T1w.json \
#       "${dst_anat}/sub-${SUB}_ses-${SES}"*_T2w.nii.gz \
#       "${dst_anat}/sub-${SUB}_ses-${SES}"*_T2w.json || true

# # Helper to extract run suffix, defaults to _run-01 when missing
# get_run_suffix() {
#   local f="$1"
#   if [[ "$f" =~ _run-[0-9]{2} ]]; then
#     grep -oE '_run-[0-9]{2}' <<<"$f" | head -n1
#   else
#     printf "%s" "_run-01"
#   fi
# }

# # Copy all matching finals (supports multiple runs)
# # T1w finals: *_acq-unidenoisedbfc*_T1w.nii.gz
# shopt -s nullglob
# t1_list=( "${src_anat}/sub-${SUB}_ses-${SES}"*_acq-unidenoisedbfc*"_T1w.nii.gz" )
# for t1_src in "${t1_list[@]}"; do
#   t1_run="$(get_run_suffix "$t1_src")"
#   t1_dst_stem="${dst_anat}/sub-${SUB}_ses-${SES}${t1_run}_T1w"
#   echo "Copying T1w:"
#   echo "  ${t1_src}  ->  ${t1_dst_stem}.nii.gz"
#   cp -av "${t1_src}" "${t1_dst_stem}.nii.gz"
#   t1_json="${t1_src%.nii.gz}.json"
#   if [[ -f "$t1_json" ]]; then
#     echo "  ${t1_json} -> ${t1_dst_stem}.json"
#     cp -av "${t1_json}" "${t1_dst_stem}.json"
#   else
#     echo "  (no T1w JSON found next to ${t1_src})"
#   fi
# done
# if (( ${#t1_list[@]} == 0 )); then
#   echo "WARN: No final T1w found matching *_acq-unidenoisedbfc*_T1w.nii.gz in ${src_anat}"
# fi

# # T2w finals: *_acq-bfc*_T2w.nii.gz
# t2_list=( "${src_anat}/sub-${SUB}_ses-${SES}"*_acq-bfc*"_T2w.nii.gz" )
# for t2_src in "${t2_list[@]}"; do
#   t2_run="$(get_run_suffix "$t2_src")"
#   t2_dst_stem="${dst_anat}/sub-${SUB}_ses-${SES}${t2_run}_T2w"
#   echo "Copying T2w:"
#   echo "  ${t2_src}  ->  ${t2_dst_stem}.nii.gz"
#   cp -av "${t2_src}" "${t2_dst_stem}.nii.gz"
#   t2_json="${t2_src%.nii.gz}.json"
#   if [[ -f "$t2_json" ]]; then
#     echo "  ${t2_json} -> ${t2_dst_stem}.json"
#     cp -av "${t2_json}" "${t2_dst_stem}.json"
#   else
#     echo "  (no T2w JSON found next to ${t2_src})"
#   fi
# done
# if (( ${#t2_list[@]} == 0 )); then
#   echo "INFO: No final T2w found matching *_acq-bfc*_T2w.nii.gz in ${src_anat}"
# fi

