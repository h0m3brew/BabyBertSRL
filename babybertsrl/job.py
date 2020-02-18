import time
import numpy as np
import pandas as pd
import attr
from pathlib import Path
import torch
import random
from itertools import chain

from allennlp.data.vocabulary import Vocabulary
from allennlp.data.iterators import BucketIterator
from allennlp.training.util import move_optimizer_to_cuda

from pytorch_pretrained_bert.tokenization import WordpieceTokenizer
from pytorch_pretrained_bert.modeling import BertModel, BertConfig
from pytorch_pretrained_bert import BertAdam

from babybertsrl import config
from babybertsrl.io import load_utterances_from_file
from babybertsrl.io import load_propositions_from_file
from babybertsrl.io import load_vocab
from babybertsrl.io import split
from babybertsrl.converter import ConverterMLM, ConverterSRL
from babybertsrl.eval import evaluate_model_on_pp
from babybertsrl.eval import predict_masked_sentences
from babybertsrl.model_mt import MTBert
from babybertsrl.eval import evaluate_model_on_f1


@attr.s
class Params(object):
    batch_size = attr.ib(validator=attr.validators.instance_of(int))
    num_layers = attr.ib(validator=attr.validators.instance_of(int))
    hidden_size = attr.ib(validator=attr.validators.instance_of(int))
    num_attention_heads = attr.ib(validator=attr.validators.instance_of(int))
    intermediate_size = attr.ib(validator=attr.validators.instance_of(int))
    num_mlm_epochs = attr.ib(validator=attr.validators.instance_of(int))
    num_srl_epochs = attr.ib(validator=attr.validators.instance_of(int))
    srl_task_ramp = attr.ib(validator=attr.validators.instance_of(int))
    srl_task_delay = attr.ib(validator=attr.validators.instance_of(int))
    num_masked = attr.ib(validator=attr.validators.instance_of(int))
    vocab_size = attr.ib(validator=attr.validators.instance_of(int))
    corpus_name = attr.ib(validator=attr.validators.instance_of(str))

    @classmethod
    def from_param2val(cls, param2val):
        """
        instantiate class.
        exclude keys from param2val which are added by Ludwig.
        they are relevant to job submission only.
        """
        kwargs = {k: v for k, v in param2val.items()
                  if k not in ['job_name', 'param_name', 'project_path', 'save_path']}
        return cls(**kwargs)


def main(param2val):

    # params
    params = Params.from_param2val(param2val)
    print(params, flush=True)

    #  paths
    project_path = Path(param2val['project_path'])
    srl_eval_path = project_path / 'perl' / 'srl-eval.pl'
    data_path_mlm = project_path / 'data' / 'training' / f'{params.corpus_name}_mlm.txt'
    data_path_srl = project_path / 'data' / 'training' / f'{params.corpus_name}_srl.txt'
    vocab_path = project_path / 'data' / f'{params.corpus_name}_vocab.txt'

    # Wordpiece tokenizer - defines input vocabulary
    vocab = load_vocab(vocab_path, params.vocab_size)
    assert vocab['[PAD]'] == 0  # AllenNLP expects this
    assert vocab['[UNK]'] == 1  # AllenNLP expects this
    assert vocab['[CLS]'] == 2
    assert vocab['[SEP]'] == 3
    assert vocab['[MASK]'] == 4
    wordpiece_tokenizer = WordpieceTokenizer(vocab)

    # load utterances for pre-training
    utterances = load_utterances_from_file(data_path_mlm)
    train_utterances, devel_utterances, test_utterances = split(utterances)

    # load propositions for fine-tuning
    propositions = load_propositions_from_file(data_path_srl)
    train_propositions, devel_propositions, test_propositions = split(propositions)

    # converters handle conversion from text to instances
    converter_mlm = ConverterMLM(params, wordpiece_tokenizer)
    converter_srl = ConverterSRL(params, wordpiece_tokenizer)

    # get output_vocab
    # note: Allen NLP vocab holds labels, wordpiece_tokenizer.vocab holds input tokens
    # what from_instances() does:
    # 1. it iterates over all instances, and all fields, and all token indexers
    # 2. the token indexer is used to update vocabulary count, skipping words whose text_id is already set
    # input tokens are not indexed, as they are already indexed by bert tokenizer vocab.
    # this ensures that the model is built with inputs for all vocab words,
    # such that words that occur only in LM or SRL task can still be input

    all_instances_mlm = chain(converter_mlm.make_instances(train_utterances),
                              converter_mlm.make_instances(devel_utterances),
                              converter_mlm.make_instances(test_utterances),
                              )
    index_vocab_mlm = Vocabulary.from_instances(all_instances_mlm)
    # index_vocab_mlm.print_statistics()

    all_instances_srl = chain(converter_srl.make_instances(train_propositions),
                              converter_srl.make_instances(devel_propositions),
                              converter_srl.make_instances(test_propositions),
                              )
    index_vocab_srl = Vocabulary.from_instances(all_instances_srl)
    # index_vocab_srl.print_statistics()

    # BERT
    print('Preparing Multi-task BERT...')
    input_vocab_size = len(converter_mlm.wordpiece_tokenizer.vocab)
    bert_config = BertConfig(vocab_size_or_config_json_file=input_vocab_size,  # was 32K
                             hidden_size=params.hidden_size,  # was 768
                             num_hidden_layers=params.num_layers,  # was 12
                             num_attention_heads=params.num_attention_heads,  # was 12
                             intermediate_size=params.intermediate_size)  # was 3072
    bert_model = BertModel(config=bert_config)

    print(index_vocab_mlm.get_vocab_size('tokens'), len(wordpiece_tokenizer.vocab))
    print(index_vocab_srl.get_vocab_size('tokens'), len(wordpiece_tokenizer.vocab))
    assert index_vocab_mlm.get_vocab_size('tokens') == index_vocab_srl.get_vocab_size('tokens')

    # BERT
    mt_bert = MTBert(vocab_mlm=index_vocab_mlm,
                     vocab_srl=index_vocab_srl,
                     bert_model=bert_model,
                     embedding_dropout=0.1)
    mt_bert.cuda()
    num_params = sum(p.numel() for p in mt_bert.parameters() if p.requires_grad)
    print('Number of model parameters: {:,}'.format(num_params), flush=True)

    # optimizers
    optimizer_mlm = BertAdam(params=mt_bert.parameters(), lr=5e-5, max_grad_norm=1.0, t_total=-1,weight_decay=0.01)
    optimizer_srl = BertAdam(params=mt_bert.parameters(), lr=5e-5, max_grad_norm=1.0, t_total=-1,weight_decay=0.01)
    move_optimizer_to_cuda(optimizer_mlm)
    move_optimizer_to_cuda(optimizer_srl)

    # batching
    bucket_batcher_mlm = BucketIterator(batch_size=params.batch_size, sorting_keys=[('tokens', "num_tokens")])
    bucket_batcher_mlm.index_with(index_vocab_mlm)
    bucket_batcher_srl = BucketIterator(batch_size=params.batch_size, sorting_keys=[('tokens', "num_tokens")])
    bucket_batcher_srl.index_with(index_vocab_srl)

    # test sentences
    test_generator_mlm = bucket_batcher_mlm(converter_mlm.make_instances(test_utterances), num_epochs=1)
    predict_masked_sentences(mt_bert, test_generator_mlm)

    # max steps (used in console only as rough estimate of progress)
    num_mlm_steps = converter_mlm.num_instances(train_utterances) // params.batch_size
    num_srl_steps = len(train_propositions) // params.batch_size
    max_step = num_mlm_steps + num_srl_steps

    # generators

    # init performance collection
    devel_pps = []
    train_pps = []
    devel_f1s = []
    train_f1s = []
    eval_steps = []
    train_start = time.time()
    loss_mlm = None
    loss_srl = None
    step = 0

    # evaluate train perplexity
    generator_mlm = bucket_batcher_mlm(converter_mlm.make_instances(train_utterances), num_epochs=1)
    train_pp = evaluate_model_on_pp(mt_bert, generator_mlm)
    train_pps.append(train_pp)
    print(f'train-pp={train_pp}', flush=True)

    # evaluate train f1
    generator_srl = bucket_batcher_srl(converter_srl.make_instances(train_propositions), num_epochs=1)
    train_f1 = evaluate_model_on_f1(mt_bert, srl_eval_path, generator_srl)
    train_f1s.append(train_f1)
    print(f'train-f1={train_f1}', flush=True)

    # generators
    train_generator_mlm = bucket_batcher_mlm(converter_mlm.make_instances(train_utterances),
                                             num_epochs=params.num_mlm_epochs)
    train_generator_srl = bucket_batcher_srl(converter_srl.make_instances(train_propositions),
                                             num_epochs=params.num_srl_epochs)

    while True:

        # TRAINING
        if step != 0:  # otherwise evaluation at step 0 is influenced by training on one batch
            mt_bert.train()

            try:
                batch_mlm = next(train_generator_mlm)
            except StopIteration:
                print('WARNING: No more MLM batches', flush=True)

            # masked language modeling task
            loss_mlm = mt_bert.train_on_batch('mlm', batch_mlm, optimizer_mlm)

            # semantic role labeling task
            if step > params.srl_task_delay:
                steps_past_delay = step - params.srl_task_delay
                prob = params.srl_task_ramp / steps_past_delay
                if random.random() > prob:
                    try:
                        batch_srl = next(train_generator_srl)
                    except StopIteration:
                        print('No more SRL batches. Exiting training', flush=True)
                        break
                    loss_srl = mt_bert.train_on_batch('srl', batch_srl, optimizer_srl)
                    print(f'Performed SRL step with loss={loss_srl}', flush=True)

        # EVALUATION
        if step % config.Eval.loss_interval == 0:
            mt_bert.eval()
            eval_steps.append(step)

            # evaluate perplexity
            devel_generator_mlm = bucket_batcher_mlm(converter_mlm.make_instances(devel_utterances),
                                                     num_epochs=1)
            devel_pp = evaluate_model_on_pp(mt_bert, devel_generator_mlm)
            devel_pps.append(devel_pp)
            print(f'devel-pp={devel_pp}', flush=True)

            # test sentences
            if config.Eval.test_sentences:
                test_generator_mlm = bucket_batcher_mlm(converter_mlm.make_instances(test_utterances),
                                                        num_epochs=1)
                predict_masked_sentences(mt_bert, test_generator_mlm)

            # evaluate devel f1
            devel_generator_srl = bucket_batcher_srl(converter_srl.make_instances(devel_propositions),
                                                     num_epochs=1)
            devel_f1 = evaluate_model_on_f1(mt_bert, srl_eval_path, devel_generator_srl)
            devel_f1s.append(devel_f1)
            print(f'devel-f1={devel_f1}', flush=True)

            # console
            min_elapsed = (time.time() - train_start) // 60
            pp = torch.exp(loss_mlm) if loss_mlm is not None else np.nan
            print(f'step {step:<6,}/{max_step:,}: pp={pp :2.4f} total minutes elapsed={min_elapsed:<3}',
                  flush=True)

        # only increment step once in each iteration of the loop, otherwise evaluation may never happen
        step += 1

    # save performance
    s1 = pd.Series(train_pps, index=np.arange(params.num_mlm_epochs))
    s1.name = 'train_pp'
    s2 = pd.Series(devel_pps, index=eval_steps)
    s2.name = 'devel_pp'
    s3 = pd.Series(train_f1s, index=np.arange(params.num_srl_epochs))
    s3.name = 'train_f1'
    s4 = pd.Series(devel_f1s, index=eval_steps)
    s4.name = 'devel_f1'

    return [s1, s2, s3, s4]
