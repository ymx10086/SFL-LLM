# 实验：对Embedding Inversion Attack进行超参搜索

dataset_label='train'
exp_name='[CR]TAG_gaussian'
global_round=1
client_steps=500
data_shrink_frac=0.08
test_data_shrink_frac=0.3
evaluate_freq=300
self_pt_enable=False
lora_at_trunk=True
lora_at_bottom=True
lora_at_top=True
collect_all_layers=True

model_names=('llama2')

sps="6-27"
batch_size=2

attacker_freq=200
attacker_samples=5
max_global_step=605
noise_mode='dxp'
noise_scale_gaussians=(6.5 7.0) # (6.0 5.5 5.0 4.5 4.0 3.5 3.0)
sfl_datasets=("piqa")
seeds=(42 7 56)

tag_lr=0.09
tag_beta=0.85
tag_epc=600

for seed in "${seeds[@]}"; do
  for model_name in "${model_names[@]}"; do
    for sfl_dataset in "${sfl_datasets[@]}"; do
      for noise_scale_gaussian in "${noise_scale_gaussians[@]}"; do
        case_name="seed${seed}-TAG@${model_name}@${sfl_dataset}-${noise_scale_gaussian}"

        # 将其用于攻击
        echo "Running evaluate_tag_methods.py with sfl_ds=$sfl_dataset"
        python ../py/sim_with_attacker.py \
          --noise_mode "$noise_mode" \
          --case_name "$case_name" \
          --seed "$seed" \
          --model_name "$model_name" \
          --split_points "$sps" \
          --global_round "$global_round" \
          --seed "$seed" \
          --dataset "$sfl_dataset" \
          --exp_name "$exp_name" \
          --sip_b2tr_enable False \
          --sip_tr2t_enable False \
          --self_pt_enable "$self_pt_enable" \
          --client_num "1" \
          --data_shrink_frac "$data_shrink_frac" \
          --test_data_shrink_frac "$test_data_shrink_frac" \
          --evaluate_freq "$evaluate_freq" \
          --client_steps "$client_steps" \
          --lora_at_top "$lora_at_top" \
          --lora_at_trunk "$lora_at_trunk" \
          --lora_at_bottom "$lora_at_bottom" \
          --collect_all_layers "$collect_all_layers" \
          --dataset_label "$dataset_label" \
          --batch_size "$batch_size" \
          --tag_enable True \
          --gma_enable False \
          --gsma_enable False \
          --sma_enable False \
          --eia_enable False --attacker_freq "$attacker_freq" \
          --attacker_samples "$attacker_samples" \
          --max_global_step "$max_global_step" \
          --tag_beta "$tag_beta" \
          --tag_lr "$tag_lr" \
          --tag_epochs "$tag_epc" \
          --noise_mode "gaussian" \
          --noise_scale_gaussian "$noise_scale_gaussian"
      done
    done
  done
done
