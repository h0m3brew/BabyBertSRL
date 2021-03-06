import numpy as np
from typing import List
from pathlib import Path
import random
from collections import OrderedDict

from babybertsrl import config


def load_childes_vocab(vocab_file, vocab_size):

    vocab = OrderedDict()
    vocab['[PAD]'] = 0
    vocab['[UNK]'] = 1
    vocab['[CLS]'] = 2
    vocab['[SEP]'] = 3
    vocab['[MASK]'] = 4
    index = 5
    with open(vocab_file, "r", encoding="utf-8") as reader:
        while len(vocab) < vocab_size + 5:
            line = reader.readline()
            if not line:
                break
            token = line.split()[1]
            vocab[token] = index
            index += 1
    return vocab


def load_vocab(childes_vocab_file, google_vocab_file, vocab_size):

    childes_vocab = set([w for w, i in load_childes_vocab(childes_vocab_file, vocab_size).items()])
    print(childes_vocab)

    vocab = OrderedDict()
    index = 0
    with open(google_vocab_file, "r", encoding="utf-8") as reader:
        while True:
            token = reader.readline()
            if not token:
                break
            if token not in childes_vocab:
                # print(token)
                continue
            token = token.strip()
            vocab[token] = index
            index += 1

    print(vocab)

    return vocab


def split(data: List, seed: int = 2):

    random.seed(seed)

    train = []
    devel = []
    test = []

    for i in data:

        if random.choices([True, False],
                          weights=[config.Data.train_prob, 1 - config.Data.train_prob])[0]:
            train.append(i)
        else:
            if random.choices([True, False], weights=[0.5, 0.5])[0]:
                devel.append(i)
            else:
                test.append(i)

    print(f'num train={len(train):,}')
    print(f'num devel={len(devel):,}')
    print(f'num test ={len(test):,}')

    return train, devel, test


def load_utterances_from_file(file_path: Path,
                              ) -> List[List[str]]:
    """
    load utterances for language modeling from text file
    """

    print(f'Loading {file_path}')

    res = []
    punctuation = {'.', '?', '!'}
    num_too_small = 0
    num_too_large = 0
    with file_path.open('r') as f:

        for line in f.readlines():

            # tokenize transcript
            transcript = line.strip().split()  # a transcript containing multiple utterances
            transcript = [w for w in transcript]

            # split transcript into utterances
            utterances = [[]]
            for w in transcript:
                utterances[-1].append(w)
                if w in punctuation:
                    utterances.append([])

            # collect utterances
            for utterance in utterances:

                # check  length
                if len(utterance) < config.Data.min_input_length:
                    num_too_small += 1
                    continue
                if len(utterance) > config.Data.max_input_length:
                    num_too_large += 1
                    continue

                res.append(utterance)

    print(f'WARNING: Skipped {num_too_small} utterances which are shorter than {config.Data.min_input_length}.')
    print(f'WARNING: Skipped {num_too_large} utterances which are larger than {config.Data.max_input_length}.')

    lengths = [len(u) for u in res]
    print('Found {:,} utterances'.format(len(res)))
    print(f'Max    utterance length: {np.max(lengths):.2f}')
    print(f'Mean   utterance length: {np.mean(lengths):.2f}')
    print(f'Median utterance length: {np.median(lengths):.2f}')
    print()

    return res


def load_propositions_from_file(file_path):
    """
    Read tokenized propositions from file.
    File format: {predicate_id} [word0, word1 ...] ||| [label0, label1 ...]
    Return:
        A list with elements of structure [[words], predicate position, [labels]]
    """

    print(f'Loading {file_path}')

    num_too_small = 0
    num_too_large = 0
    res = []
    with file_path.open('r') as f:

        for line in f.readlines():

            inputs = line.strip().split('|||')
            left_input = inputs[0].strip().split()
            right_input = inputs[1].strip().split()

            # predicate
            predicate_index = int(left_input[0])

            # words + labels
            words = left_input[1:]
            labels = right_input

            # check  length
            if len(words) <= config.Data.min_input_length:
                num_too_small += 1
                continue
            if len(words) > config.Data.max_input_length:
                num_too_large += 1
                continue

            res.append((words, predicate_index, labels))

    print(f'WARNING: Skipped {num_too_small} propositions which are shorter than {config.Data.min_input_length}.')
    print(f'WARNING: Skipped {num_too_large} propositions which are larger than {config.Data.max_input_length}.')

    lengths = [len(p[0]) for p in res]
    print('Found {:,} propositions'.format(len(res)))
    print(f'Max    proposition length: {np.max(lengths):.2f}')
    print(f'Mean   proposition length: {np.mean(lengths):.2f}')
    print(f'Median proposition length: {np.median(lengths):.2f}')
    print()

    return res
