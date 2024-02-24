from abc import ABC, abstractmethod

import torch
from datasets import load_dataset, disable_progress_bar
from torch.utils.data import DataLoader

from sfl import config
from sfl.utils.data import random_slicing


class FedDataset(ABC):
    """
    联邦数据集
    """

    def __init__(self, tokenizer, client_ids: list[str], dataset, types: list[str], shrink_frac=1.0, task_type='lm',
                 num_labels=0):
        self.tokenizer = tokenizer
        self.client_ids = client_ids
        self.client_data_indices = {}
        self.all_dataset = dataset
        self.dataset = {}
        self.task_type = task_type
        self.num_labels = num_labels
        for type in types:
            self.dataset[type] = self.all_dataset[type].select(range(int(len(self.all_dataset[type]) * shrink_frac)))
            sliced = random_slicing(range(len(self.dataset[type])), len(client_ids), sgm=0.15)
            disable_progress_bar()
            self.client_data_indices[type] = {cid: sliced[i] for i, cid in enumerate(client_ids)}

    def get_dataloader(self, client_id, batch_size=1, type='train'):
        ds = self.dataset[type].select(self.client_data_indices[type][client_id])
        return DataLoader(self._pre_process(ds, batch_size),
                          collate_fn=lambda x: self._col_fun(x),
                          batch_size=batch_size,
                          shuffle=True)

    def get_dataloader_unsliced(self, batch_size=2, type='train', shrink_frac=1.0, further_test_split=None):
        ds = self.all_dataset[type].select(range(int(len(self.all_dataset[type]) * shrink_frac)))
        if further_test_split is not None:
            ds_split = ds.train_test_split(shuffle=True, test_size=further_test_split)
            return DataLoader(self._pre_process(ds_split['train'], batch_size),
                              collate_fn=lambda x: self._col_fun(x),
                              batch_size=batch_size,
                              shuffle=True), \
                   DataLoader(self._pre_process(ds_split['test'], batch_size),
                              collate_fn=lambda x: self._col_fun(x),
                              batch_size=batch_size, shuffle=True)
        return DataLoader(self._pre_process(ds, batch_size), batch_size=batch_size, shuffle=True,
                          collate_fn=lambda x: self._col_fun(x))

    def _pre_process(self, ds, batch_size):
        ds = ds.map(lambda x: self._format(x), batched=False)
        ds.set_format(type="torch")
        return ds

    def _col_fun(self, batch):
        texts = [b['input'] for b in batch]
        input = self.tokenizer(texts, padding=True, truncation=True, return_tensors='pt')
        return {'input_ids': input['input_ids'],
                'input_att_mask': input['attention_mask'],
                'input_text': texts}

    @abstractmethod
    def _format(self, example):
        raise NotImplementedError


class PIQAFedDataset(FedDataset):
    """
    PIQA数据集
    """

    def __init__(self, tokenizer, client_ids: list[str], shrink_frac: float = 0.3):
        super().__init__(tokenizer, client_ids, dataset=load_dataset('piqa', cache_dir=config.dataset_cache_dir),
                         types=['train', 'test', 'validation'],
                         shrink_frac=shrink_frac)

    def _format(self, example):
        q = "### Question: " + example["goal"]
        a = "### Solution: " + example["sol1"]
        return {'q': q, 'a': a,
                'input': q + "\n" + a}


class GSM8KFedDataset(FedDataset):

    def __init__(self, tokenizer, client_ids: list[str], shrink_frac: float = 0.3):
        super().__init__(tokenizer, client_ids,
                         load_dataset(config.dataset_cache_dir + 'gsm8k', 'main'),
                         types=['train', 'test'], shrink_frac=shrink_frac)

    def _format(self, example):
        q = "### Question: " + example['question']
        a = "### Answer: " + example['answer']
        return {'q': q, 'a': a,
                'input': q + "\n" + a}


class DialogSumFedDataset(FedDataset):

    def __init__(self, tokenizer, client_ids: list[str], shrink_frac: float = 0.3):
        super().__init__(tokenizer, client_ids,
                         load_dataset(config.dataset_cache_dir + 'dialogsum'),
                         ['train', 'test', 'validation'],
                         shrink_frac)

    def _format(self, example):
        q = "### Dialogue: " + example["dialogue"]
        a = "### Summary: " + example["summary"]
        return {'q': q, 'a': a,
                'input': q + "\n" + a}

    def _col_fun(self, batch):
        texts = [b['input'] for b in batch]
        input = self.tokenizer(texts, padding=True, truncation=True, max_length=512,
                               return_tensors='pt')
        return {'input_ids': input['input_ids'],
                'input_att_mask': input['attention_mask'],
                'input_text': texts}


class CodeAlpacaFedDataset(FedDataset):

    def __init__(self, tokenizer, client_ids: list[str], shrink_frac: float = 0.3):
        super().__init__(tokenizer, client_ids, load_dataset(config.dataset_cache_dir + 'CodeAlpaca_20K'),
                         ['train', 'test'],
                         shrink_frac)

    def _format(self, example):
        q = "### Question: " + example['prompt']
        a = "### Answer: " + example['completion']
        return {'q': q, 'a': a,
                'input': q + "\n" + a}

    def _col_fun(self, batch):
        texts = [b['input'] for b in batch]
        input = self.tokenizer(texts, padding=True, truncation=True, max_length=512,
                               return_tensors='pt')
        return {'input_ids': input['input_ids'],
                'input_att_mask': input['attention_mask'],
                'input_text': texts}


class IMDBFedDataset(FedDataset):

    def __init__(self, tokenizer, client_ids: list[str], shrink_frac: float = 0.3):
        super().__init__(tokenizer, client_ids, load_dataset('imdb', cache_dir=config.dataset_cache_dir),
                         ['train', 'test', 'unsupervised'],
                         shrink_frac, task_type='clsf', num_labels=2)

    def _format(self, example):
        return {'input': example['text']}

    def _col_fun(self, batch):
        texts = [b['input'] for b in batch]
        input = self.tokenizer(texts, padding=True, truncation=True, return_tensors='pt', max_length=512)
        # convert labels to tensor
        labels = [b['label'] for b in batch]
        labels = torch.tensor(labels)
        return {'input_ids': input['input_ids'],
                'input_att_mask': input['attention_mask'],
                'input_text': texts, 'labels': labels}


class WikiTextFedDataset(FedDataset):

    def _format(self, example):
        pass

    def __init__(self, tokenizer, client_ids: list[str], shrink_frac=0.3):
        dataset = load_dataset(config.dataset_cache_dir + 'wikitext', 'wikitext-2-v1')
        types = ['train', 'test', 'validation']
        super().__init__(tokenizer, client_ids, dataset, types, shrink_frac)

    def _pre_process(self, ds, batch_size):
        def tokenize_function(examples):
            return self.tokenizer(examples["text"])

        tokenized_datasets = ds.map(
            tokenize_function, batched=True, num_proc=4, remove_columns=["text"]
        )
        block_size = 128

        def group_texts(examples):
            # 连接所有文本。
            concatenated_examples = {k: sum(examples[k], []) for k in examples.keys()}
            total_length = len(concatenated_examples[list(examples.keys())[0]])
            # 我们丢弃小的余数，但如果模型支持的话，您可以添加填充
            # 在这一点上，就像在所有事情上一样，我们建议您跟随自己的内心
            total_length = (total_length // block_size) * block_size
            # 按 max_len 分割。
            result = {
                k: [t[i: i + block_size] for i in range(0, total_length, block_size)]
                for k, t in concatenated_examples.items()
            }
            result["labels"] = result["input_ids"].copy()
            result["input_text"] = [self.tokenizer.decode(ii) for ii in result["input_ids"]]
            result["input_att_mask"] = result["attention_mask"]
            return result

        lm_datasets = tokenized_datasets.map(
            group_texts,
            batched=True,
            batch_size=batch_size,
            num_proc=4,
        )
        lm_datasets.set_format(type="torch")
        return lm_datasets

    def _col_fun(self, batch):
        res = {}
        for k in batch[0].keys():
            ls = [x[k] for x in batch]
            if isinstance(ls[0], torch.Tensor):
                res[k] = torch.stack(ls)
            else:
                res[k] = ls
        return res
