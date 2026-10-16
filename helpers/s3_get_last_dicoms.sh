#!/bin/bash

# Author: Thomas Madison (local copy: loop filtered to DIR lines only)
# Description: "s3cmd get" the last DICOM file (as sorted by "s3cmd ls") from each subdir of the
#   specified input dir. (Both the subdir and file are downloaded to the destination path.) 

# Default values for input and output paths
input_path=""
output_path=""

# Function to display usage information
usage() {
  echo "Usage: $0 -i <input_path> -o <output_path>"
  exit 1
}

# Parsing command line options
while getopts ":i:o:" opt; do
  case ${opt} in
    i)
      input_path=$OPTARG
      ;;
    o)
      output_path=$OPTARG
      ;;
    \?)
      echo "Invalid option: $OPTARG" 1>&2
      usage
      ;;
    :)
      echo "Option -$OPTARG requires an argument." 1>&2
      usage
      ;;
  esac
done
shift $((OPTIND -1))

# Check if both input and output paths are provided
if [ -z "$input_path" ] || [ -z "$output_path" ]; then
  echo "Both input and output paths are required."
  usage
fi

# Execute the command
# 1) s3cmd ls to get list of run dirs
# 2) loop over run dirs; make output subdir and s3cmd get the last file the run dir into the output subdir
#    (only "DIR" lines: skips S3 folder-marker objects such as the "DIROBJ" entry for the input dir itself)
for n in $(s3cmd ls "$input_path" | awk '$1=="DIR" { print $NF }'); do
  echo ${n}
  mkdir -p "${output_path}"/$(basename "${n}")
  s3cmd get $(s3cmd ls "${n}" | tail -1 | awk '{print $NF}') "${output_path}"/$(basename "${n}/") --skip-existing
done
