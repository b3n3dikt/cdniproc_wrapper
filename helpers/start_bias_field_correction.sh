#!/bin/bash

#SBATCH -J biasfield
#SBATCH --ntasks=12
#SBATCH --tmp=20gb
#SBATCH --mem=10gb
#SBATCH -t 1:00:00
#SBATCH -p msismall
#SBATCH -A bart

#module load fsl
module load afni
echo "Starting process..."

# Get the input file path from the first script argument
infile="${1}"
outfile="${2}"
pCodePath="${3}"



# Extract the directory and base filename (without extension)
indir=$(dirname "${infile}")
base=$(basename "${infile}" .nii.gz)  # Assuming the infile ends with .nii.gz

work=/tmp/biasfield/${base}

if [ ! -d ${work} ]; then
    mkdir -p ${work}
fi

cp ${infile} ${work}

pushd ${work}
# Prepare the output file path by appending "_bico" before the .nii.gz extension
#outfile="${indir}/${base}_bfc.nii.gz"
#pushd ${indir}
cp ${pCodePath}/Bias_field_script_job.m ${work}

#cp /panfs/jay/groups/34/bart/shared/projects/7Tpiloting/anat_testing/scripts/Bias_field_script_job.m ${indir}
# Use 3dcalc to copy the input to a temporary uncompressed NIfTI file (intermediate step)
3dcalc -a "${infile}" -expr 'a' -prefix uncorr.nii -overwrite

echo "Running MATLAB SPM bias field correction..."
# Assuming Bias_field_script_job is correctly set up to use uncorr.nii and save the output as muncorr.nii
matlab -nodesktop -nosplash -r "Bias_field_script_job; exit"

# Use 3dcalc to ensure the data format, work with muncorr.nii, and save the output
3dcalc -a muncorr.nii -expr 'a' -prefix muncorr.nii -overwrite -datum short

# Compress the corrected file to the desired output path
3dcalc -a muncorr.nii -expr 'a' -prefix "${outfile}" -overwrite

rm -f Bias_field_script_job.m
rm -f *uncorr* 
#mv muncorr.nii.gz "${outfile}"

# Clean up intermediate uncompressed file if needed
#rm -f uncorr.nii
#rm -f muncorr.nii
popd 

echo "Process completed successfully."

# #module load fsl
# module load afni
# echo "Starting process..."

# # Get the input file path from the first script argument
# infile="${1}"

# # Extract the directory, filename, and extension from the infile
# indir=$(dirname "${infile}")
# infile_name=$(basename "${infile}")
# filename="${infile_name%.*}"
# extension="${infile_name##*.}"

# # Prepare the output file path by appending "_bico" before the extension
# outfile="${indir}/${filename}_bico.${extension}"

# # Process the input file and save it as uncorr.nii in the current directory
# #fslmaths "${infile}" -mul 1 uncorr.nii
# #cp "${infile}" uncorr.nii.gz

# 3dcalc -a "${infile}" -expr 'a' -prefix uncorr.nii

# echo "Running MATLAB SPM bias field correction..."
# # Assuming Bias_field_script_job is correctly set up to use uncorr.nii and save the output as muncorr.nii
# matlab -nodesktop -nosplash -r "Bias_field_script_job; exit"

# # Use 3dcalc to ensure the data format and overwrite the muncorr.nii
# 3dcalc -a muncorr.nii -prefix muncorr.nii -overwrite -expr 'a' -datum short

# # Move the corrected file to the output path
# mv muncorr.nii "${outfile}"

# # Clean up intermediate file
# rm uncorr.nii

# echo "Process completed successfully."


# module load fsl
# echo "fange an"
# infile=${1}
# #cp $1 uncorr.nii
# fslmaths ${infile} -mul 1 uncorr.nii
# echo "get SPM bias field batch"
# #cp /home/bart/bart/software/layerfMRI/repository/bias_field_corr/Bias_field_script_job.m ./Bias_field_script_job.m
# matlab -nodesktop -nosplash -r "Bias_field_script_job"

# 3dcalc -a muncorr.nii -prefix muncorr.nii -overwrite -expr 'a' -datum short

# mv muncorr.nii bico_${infile}

# rm uncorr.nii

# #rm c*uncorr.nii

# echo "und tschuess"
