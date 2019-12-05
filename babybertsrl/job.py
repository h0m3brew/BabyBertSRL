import time
import numpy as np
import pandas as pd
import attr
from pathlib import Path
import torch
from itertools import chain

from allennlp.data.vocabulary import Vocabulary
from allennlp.data.iterators import BucketIterator
from allennlp.training.util import move_optimizer_to_cuda

from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.modeling import BertModel, BertConfig
from pytorch_pretrained_bert import BertAdam

from babybertsrl import config
from babybertsrl.data_lm import DataLM
from babybertsrl.data_srl import DataSRL
from babybertsrl.eval import evaluate_model_on_pp
from babybertsrl.eval import predict_masked_sentences
from babybertsrl.model_lm import LMBert
from babybertsrl.model_srl import SrlBert
from babybertsrl.eval import evaluate_model_on_f1


@attr.s
class Params(object):
    batch_size = attr.ib(validator=attr.validators.instance_of(int))
    num_layers = attr.ib(validator=attr.validators.instance_of(int))
    hidden_size = attr.ib(validator=attr.validators.instance_of(int))
    num_attention_heads = attr.ib(validator=attr.validators.instance_of(int))
    intermediate_size = attr.ib(validator=attr.validators.instance_of(int))
    max_sentence_length = attr.ib(validator=attr.validators.instance_of(int))
    num_pre_train_epochs = attr.ib(validator=attr.validators.instance_of(int))
    num_fine_tune_epochs = attr.ib(validator=attr.validators.instance_of(int))
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
    train_data_path_lm = project_path / 'data' / 'CHILDES' / f'{params.corpus_name}_train_lm.txt'
    devel_data_path_lm = project_path / 'data' / 'CHILDES' / f'{params.corpus_name}_devel_lm.txt'
    test_data_path_lm = project_path / 'data' / 'CHILDES' / f'{params.corpus_name}_test_lm.txt'
    train_data_path_srl = project_path / 'data' / 'CHILDES' / f'{params.corpus_name}_train_srl.txt'
    devel_data_path_srl = project_path / 'data' / 'CHILDES' / f'{params.corpus_name}_devel_srl.txt'
    vocab_path = project_path / 'data' / f'{params.corpus_name}_vocab_{params.vocab_size}.txt'

    # BERT tokenizer - defines input vocabulary
    bert_tokenizer = BertTokenizer(str(vocab_path),
                                   do_basic_tokenize=False,
                                   do_lower_case=False)  # set to false because [MASK] must be uppercase

    # load utterances for pre-training
    train_data_lm = DataLM(params, train_data_path_lm, bert_tokenizer)
    devel_data_lm = DataLM(params, devel_data_path_lm, bert_tokenizer)
    test_data_lm = DataLM(params, test_data_path_lm, bert_tokenizer)

    # load propositions for fine-tuning on SRL task
    train_data_srl = DataSRL(params, train_data_path_srl, bert_tokenizer)
    devel_data_srl = DataSRL(params, devel_data_path_srl, bert_tokenizer)

    # get output_vocab
    # note: Allen NLP vocab holds labels, bert_tokenizer.vocab holds input tokens
    # what from_instances() does:
    # 1. it iterates over all instances, and all fields, and all token indexers
    # 2. the token indexer is used to update vocabulary count, skipping words whose text_id is already set
    # input tokens are not indexed, as they are already indexed by bert tokenizer vocab.
    # this ensures that the model is built with inputs for all vocab words,
    # such that words that occur only in LM or SRL task can still be input
    all_instances_lm = chain(train_data_lm.instances, devel_data_lm.instances)
    output_vocab_lm = Vocabulary.from_instances(all_instances_lm)
    output_vocab_lm.print_statistics()

    all_instances_srl = chain(train_data_srl.instances, devel_data_srl.instances)
    output_vocab_srl = Vocabulary.from_instances(all_instances_srl)
    output_vocab_srl.print_statistics()

    # BERT  # TODO original implementation used slanted_triangular learning rate scheduler
    # parameters of original implementation are specified here:
    # https://github.com/allenai/allennlp/blob/master/training_config/bert_base_srl.jsonnet
    print('Preparing BERT for pre-training...')
    input_vocab_size = len(train_data_lm.bert_tokenizer.vocab)
    bert_config = BertConfig(vocab_size_or_config_json_file=input_vocab_size,  # was 32K
                             hidden_size=params.hidden_size,  # was 768
                             num_hidden_layers=params.num_layers,  # was 12
                             num_attention_heads=params.num_attention_heads,  # was 12
                             intermediate_size=params.intermediate_size)  # was 3072
    bert_model = BertModel(config=bert_config)

    # TODO how does PADDING get represented in model?
    # TODO Allen NLP padding symbol is different from [PAD]

    # BERT + LM head
    bert_lm = LMBert(vocab=output_vocab_lm,
                     bert_model=bert_model,
                     embedding_dropout=0.1)
    bert_lm.cuda()
    num_params = sum(p.numel() for p in bert_lm.parameters() if p.requires_grad)
    print('Number of model parameters: {:,}'.format(num_params), flush=True)
    optimizer_lm = BertAdam(params=bert_lm.parameters(),
                            lr=5e-5,
                            max_grad_norm=1.0,
                            t_total=-1,
                            weight_decay=0.01)
    move_optimizer_to_cuda(optimizer_lm)

    # ///////////////////////////////////////////
    # pre train
    # ///////////////////////////////////////////

    # batcher
    bucket_batcher = BucketIterator(batch_size=params.batch_size, sorting_keys=[('tokens', "num_tokens")])
    bucket_batcher.index_with(output_vocab_lm)

    predict_masked_sentences(bert_lm, test_data_lm, output_vocab_lm)

    devel_pps = []
    train_pps = []
    eval_steps = []
    train_start = time.time()
    for epoch in range(params.num_pre_train_epochs):
        print(f'\nEpoch: {epoch}', flush=True)

        # evaluate train perplexity
        train_pp = None
        train_pps.append(train_pp)
        print(f'train-pp={train_pp}', flush=True)

        # train
        bert_lm.train()
        train_generator = bucket_batcher(train_data_lm.make_instances(train_data_lm.utterances), num_epochs=1)
        for step, batch in enumerate(train_generator):
            loss = bert_lm.train_on_batch(batch, optimizer_lm)

            if step % config.Eval.loss_interval == 0:

                # evaluate perplexity
                instances_generator = bucket_batcher(devel_data_lm.make_instances(devel_data_lm.utterances), num_epochs=1)
                devel_pp = evaluate_model_on_pp(bert_lm, instances_generator)
                devel_pps.append(devel_pp)
                eval_steps.append(step)
                print(f'devel-pp={devel_pp}', flush=True)

                # test sentences
                predict_masked_sentences(bert_lm, test_data_lm, output_vocab_lm)  # TODO save results to file

                # console
                min_elapsed = (time.time() - train_start) // 60
                print(f'step {step:<6}: pp={torch.exp(loss):2.4f} total minutes elapsed={min_elapsed:<3}', flush=True)

    # to pandas
    s1 = pd.Series(train_pps, index=np.arange(params.num_pre_train_epochs))
    s1.name = 'train_pp'
    s2 = pd.Series(devel_pps, index=eval_steps)
    s2.name = 'devel_pp'

    # ///////////////////////////////////////////
    # fine-tune
    # ///////////////////////////////////////////

    # batcher
    bucket_batcher = BucketIterator(batch_size=params.batch_size, sorting_keys=[('tokens', "num_tokens")])
    bucket_batcher.index_with(output_vocab_srl)

    print('Preparing BERT for fine-tuning...')
    bert_srl = SrlBert(vocab=output_vocab_srl,
                       bert_model=bert_model,  # bert_model is reused 
                       embedding_dropout=0.1)
    bert_srl.cuda()

    num_params = sum(p.numel() for p in bert_srl.parameters() if p.requires_grad)
    print('Number of model parameters: {:,}'.format(num_params), flush=True)
    optimizer_srl = BertAdam(params=bert_srl.parameters(),
                             lr=5e-5,
                             max_grad_norm=1.0,
                             t_total=-1,
                             weight_decay=0.01)

    devel_f1s = []
    train_f1s = []
    eval_steps = []
    train_start = time.time()
    for epoch in range(params.num_fine_tune_epochs):
        print(f'\nEpoch: {epoch}', flush=True)

        # evaluate train f1
        train_f1 = None
        train_f1s.append(train_f1)
        print(f'train-f1={train_f1}', flush=True)

        # train
        bert_srl.train()
        train_generator = bucket_batcher(train_data_srl.make_instances(train_data_srl.propositions),
                                         num_epochs=1)
        for step, batch in enumerate(train_generator):
            loss = bert_srl.train_on_batch(batch, optimizer_srl)

            if step % config.Eval.loss_interval == 0:
                # evaluate devel f1
                instances_generator = bucket_batcher(devel_data_srl.make_instances(devel_data_srl.propositions),
                                                     num_epochs=1)
                devel_f1 = evaluate_model_on_f1(bert_srl, srl_eval_path, instances_generator)
                devel_f1s.append(devel_f1)
                eval_steps.append(step)
                print(f'devel-f1={devel_f1}', flush=True)

                # console
                min_elapsed = (time.time() - train_start) // 60
                print(f'step {step:<6}: loss={loss:2.4f} total minutes elapsed={min_elapsed:<3}', flush=True)

    # to pandas
    s3 = pd.Series(train_f1s, index=np.arange(params.num_fine_tune_epochs))
    s3.name = 'train_f1'
    s4 = pd.Series(devel_f1s, index=eval_steps)
    s4.name = 'devel_f1'

    return [s1, s2, s3, s4]