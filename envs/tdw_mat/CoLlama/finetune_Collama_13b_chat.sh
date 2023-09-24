# We use 8 x 6 = 48 V100-32GB GPUs
# On AiMOS cluster [https://docs.cci.rpi.edu/clusters/DCS_Supercomputer/]
# salloc --nodes 16 --time 6:00:00 --gres=gpu:32g:6 srun bash finetune_dromedary_2_70b_lora.sh

# Due to some unknown issues in HF datasets library, we recommend run `finetune.py`
# with --fake_run flag to prepare the dataset on your local machine,
# and then submit the slurm training job to the cluster.
set -e
set -x

export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5
export MODEL_DIR="/gpfs/u/home/AICD/AICDhnng/scratch/Collama"
export DATA_DIR="/gpfs/u/home/AICD/AICDhnng/scratch/Collama"
export PYTHONPATH="$PWD:$PYTHONPATH"
export GPUS_PER_NODE=6
export MASTER_ADDR=$(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1)
export MASTER_PORT=9901
export TOTAL_NUM_GPUS=$(($SLURM_NNODES * $GPUS_PER_NODE))

verbose_value=$(($SLURM_PROCID == 0))

if [ $verbose_value -eq 1 ]; then
    verbose_output=""
else
    verbose_output="--disable_verbose True"
fi

TOTAL_BATCH_SIZE=768
LEARNING_RATE=4e-4
NUM_EPOCHS=1
CKPT_STEPS=50

MICRO_BATCH_SIZE=8
GRADIENT_ACCUMULATION_STEPS=$(($TOTAL_BATCH_SIZE / $MICRO_BATCH_SIZE / $TOTAL_NUM_GPUS))

source /gpfs/u/home/AICD/AICDsnzh/.bashrc
conda activate /gpfs/u/home/AICD/AICDsnzh/scratch/conda_envs/dromedary_ppc

accelerate launch \
    --num_processes=$TOTAL_NUM_GPUS --num_machines=$SLURM_NNODES --machine_rank=$SLURM_PROCID \
    --main_process_ip $MASTER_ADDR --main_process_port $MASTER_PORT \
    --deepspeed_multinode_launcher "standard" \
    finetune.py \
    --num_warmup_steps 100 \
    --batch_size $TOTAL_BATCH_SIZE \
    --micro_batch_size $MICRO_BATCH_SIZE \
    --learning_rate $LEARNING_RATE \
    --num_epochs $NUM_EPOCHS \
    --ds_gradient_accumulation_steps $GRADIENT_ACCUMULATION_STEPS \
    --base_model "/gpfs/u/home/AICD/AICDsnzh/scratch-shared/llama_zf/llama-2-13b-chat-hf" \
    --output_dir "$MODEL_DIR/Collama-13b-chat-lora" \
    --run_tensorboard_dir True \
    --checkpointing_steps $CKPT_STEPS \
    --resume_from_checkpoint True \
    --data_path "$DATA_DIR/data.json" \
    --cutoff_len 1024 \
    --train_on_inputs False \
    --lora_target_modules='[q_proj,k_proj,v_proj,o_proj,gate_proj,down_proj,up_proj]' \
    --lora_r=64