import argparse
from functools import wraps
import numpy as np
import os
from time import time
import torch
from tqdm import tqdm

from torch.utils.data.dataloader import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler
from transformers.data.data_collator import default_data_collator
from transformers import AutoTokenizer, BertForMaskedLM

from adaptive_sampler import MaxTokensBatchSampler, data_collator_for_adaptive_sampler
from data_processor import CORDDataset

from apex import amp

TOP_N_WORDS = 100
PAD_ID = 0

def main(args):
    outfiles = {'in_word': open(os.path.join(args.preds_dir, 'in_word.npy'), 'wb'),
                'sent_offset': open(os.path.join(args.preds_dir, 'sent_offset.npy'), 'wb'),
                'word_offset': open(os.path.join(args.preds_dir, 'word_offset.npy'), 'wb'),
                'preds': open(os.path.join(args.preds_dir, 'preds.npy'), 'wb'),
                'probs': open(os.path.join(args.preds_dir, 'probs.npy'), 'wb')}
    
    device = torch.device("cuda:0" if torch.cuda.is_available() and not args.cpu else "cpu")
    tokenizer, model = initialize_models(device, args)

    dataset = CORDDataset(args, tokenizer)
    dataloader = simple_dataloader(args, dataset) if args.simple_sampler else adaptive_dataloader(args, dataset)

    before = time()
    total_num_of_tokens = 0
    for inputs in tqdm(dataloader):
        total_num_of_tokens += inputs['attention_mask'].sum()
        with torch.no_grad():
            dict_to_device(inputs, device)
            sent_offsets = inputs.pop('guid')
            last_hidden_states = model(**inputs)[0]
            normalized = last_hidden_states.softmax(-1)
            probs, indices = normalized.topk(TOP_N_WORDS)

            write_preds_to_file(outfiles, sent_offsets, inputs, indices, probs)

    log_times(args, time() - before, total_num_of_tokens)
    
    for f in outfiles.values():
        f.close()

def initialize_models(device, args):
    tokenizer = AutoTokenizer.from_pretrained('allenai/scibert_scivocab_uncased')
    model = BertForMaskedLM.from_pretrained('allenai/scibert_scivocab_uncased')
    model.to(device)
    if args.fp16:
        model = amp.initialize(model, opt_level="O1")
    assert tokenizer.vocab_size < 32767 #Saving subs as np.int16
    return tokenizer, model

def adaptive_dataloader(args, dataset):
    sampler = MaxTokensBatchSampler(dataset, max_tokens=args.max_tokens_per_batch, padding_noise=0.0)
    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        sampler=sampler,
        collate_fn=data_collator_for_adaptive_sampler,
    )
    return dataloader

def simple_dataloader(args, dataset):
    sampler = (
        RandomSampler(dataset)
        if args.local_rank == -1
        else DistributedSampler(dataset)
    )

    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        sampler=sampler,
        collate_fn=default_data_collator,
    )
    return dataloader

def write_preds_to_file(outfiles, sent_offsets, inputs, preds, probs):
    b, l = inputs['input_ids'].size()
    attention_mask = inputs['attention_mask'].bool()
    
    sent_offsets = sent_offsets.unsqueeze(1).expand((b, l)).masked_select(attention_mask).cpu().numpy()
    word_offsets = torch.arange(0, l, device=attention_mask.device).repeat(b, 1).masked_select(attention_mask).cpu().numpy()
    tokens = inputs['input_ids'].masked_select(attention_mask).cpu().numpy()
    preds = preds.masked_select(attention_mask.unsqueeze(2)).view(-1, TOP_N_WORDS).cpu().numpy()
    probs = probs.masked_select(attention_mask.unsqueeze(2)).view(-1, TOP_N_WORDS).cpu().numpy()
    for in_word, sent_offset, word_offset, subs, sub_probs in zip(tokens, sent_offsets, word_offsets, preds, probs):
        write(outfiles, in_word, sent_offset, word_offset, subs, sub_probs)

def write(outfiles, in_word, sent_offset, word_offset, subs, sub_probs):
    np.save(outfiles['in_word'], np.array(in_word).astype(np.int16))
    np.save(outfiles['sent_offset'], np.array(sent_offset).astype(np.int32))
    np.save(outfiles['word_offset'], np.array(word_offset).astype(np.int16))
    np.save(outfiles['preds'], subs.astype(np.int16))
    np.save(outfiles['probs'], sub_probs.astype(np.float32))

def dict_to_device(inputs, device):
    for k, v in inputs.items():
        if isinstance(v, torch.Tensor):
            inputs[k] = v.to(device)

def log_times(args, time_took, total_num_of_tokens):
    filename = os.path.join("benchmarks", f"timing_{str(time()).split('.')[0]}.txt")
    with open(filename, 'w') as f:
        f.write(os.path.basename(__file__).split('.')[0])
        for k, v in vars(args).items():
            f.write(f"{k}: {v}\n")
        f.write(f"Token per second: {total_num_of_tokens/time_took}\n")
        f.write(f"Total hours: {time_took/60/60}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--data_dir", type=str, required=True)
    parser.add_argument("--preds_dir", type=str, default="preds")
    parser.add_argument("--local_rank", type=int, default=-1)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--max_seq_length", type=int, default=512)
    parser.add_argument("--max_tokens_per_batch", type=int, default=-1)
    parser.add_argument("--overwrite_cache", action="store_true")
    parser.add_argument("--simple_sampler", action="store_true")
    parser.add_argument("--iterativly_go_over_matrices", action="store_true")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--fp16", action="store_true")

    args = parser.parse_args()
    if args.simple_sampler:
        assert args.max_tokens_per_batch == -1 and \
            args.batch_size > 1 and \
            args.max_seq_length > -1, \
            "Expecting arguments for simple sampler"
    else:
        assert args.max_tokens_per_batch > 0 and \
            args.batch_size == 1 and \
            "Expecting arguments for adaptive sampler"

    main(args)