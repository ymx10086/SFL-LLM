import argparse
import os
import sys

import wandb

sys.path.append(os.path.abspath('../..'))
from experiments.scripts.basic_strategy import BaseSFLStrategy
from sfl.utils.experiments import add_sfl_params
from sfl.model.dlgattacker import GPT2TopDLGAttacker
from sfl.simulator.simulator import SFLSimulator
from sfl.config import FLConfig
from sfl.utils.training import set_random_seed, get_dataset_class, get_attacker_class, extract_attacker_path, \
    get_model_and_tokenizer


def sfl_with_attacker(args):
    model, tokenizer  = get_model_and_tokenizer(args.model_name,args.task_type)
    # 加载攻击模型
    attacker1, attacker2 = extract_attacker_path(args, get_attacker_class(args.attacker_model))
    # 配置联邦学习
    client_ids = [str(i) for i in range(args.client_num)]
    config = FLConfig(global_round=args.global_round,
                      client_evaluate_freq=args.evaluate_freq,
                      client_steps=args.client_steps,
                      client_epoch=args.client_epoch,  # 每轮联邦每个Client训x轮
                      split_point_1=int(args.split_points.split('-')[0]),
                      split_point_2=int(args.split_points.split('-')[1]),
                      use_lora_at_trunk=args.lora_at_trunk,  # 在trunk部分使用LoRA
                      use_lora_at_top=args.lora_at_top,
                      use_lora_at_bottom=args.lora_at_bottom,
                      top_and_bottom_from_scratch=args.client_from_scratch,  # top和bottom都不采用预训练参数.
                      noise_mode=args.noise_mode,
                      noise_scale=args.noise_scale,  # 噪声大小
                      collect_intermediates=True,
                      )
    # 加载TAG攻击模型
    mocker = None
    if args.dlg_enable:
        # !需要在LoRA加上去之前进行复制
        mocker = GPT2TopDLGAttacker(config, model)
    # 加载数据集
    dataset_cls = get_dataset_class(args.dataset)
    fed_dataset = dataset_cls(tokenizer=tokenizer, client_ids=client_ids, shrink_frac=args.data_shrink_frac)
    test_dataset = dataset_cls(tokenizer=tokenizer, client_ids=[])
    test_loader = test_dataset.get_dataloader_unsliced(1, 'test', shrink_frac=args.test_data_shrink_frac)
    simulator = SFLSimulator(client_ids=client_ids,
                             strategy=BaseSFLStrategy(args, tokenizer, attacker1, attacker2, model, test_loader),
                             llm=model,
                             tokenizer=tokenizer,
                             dataset=fed_dataset, config=config)
    wandb.init(
        project=args.exp_name,
        name=f"{args.split_points}",
        config=args
    )
    simulator.strategy.dlg = mocker
    simulator.simulate()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    add_sfl_params(parser)
    args = parser.parse_args()
    set_random_seed(args.seed)
    sfl_with_attacker(args)
