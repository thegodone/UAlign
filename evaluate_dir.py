from utils.chemistry_parse import canonical_smiles, clear_map_number
import json
import numpy as np
import argparse
from tqdm import tqdm
import os

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--path', required=True, type=str,
        help='the path for file storing result'
    )
    parser.add_argument(
        '--beam', type=int, default=10,
        help='the number of beams for searching'
    )

    args = parser.parse_args()

    answers, targs = [], None
    for x in os.listdir(args.path):
        if x.endswith('.json'):
            with open(os.path.join(args.path, x)) as Fin:
                INFO = json.load(Fin)
            targs = INFO['args']
            answers.extend(INFO['answer'])

    topks = []
    for single in tqdm(answers):
        reac, prod = single['query'].split('>>')
        real_ans = clear_map_number(reac)
        opt = np.zeros(args.beam)
        for idx, x in enumerate(single['answer']):
            x = canonical_smiles(x)
            if x == real_ans:
                opt[idx:] = 1
                break
        topks.append(opt)
    topks = np.stack(topks, axis=0)
    topk_acc = np.mean(topks, axis=0)

    print(f'[args]\n{targs}')
    for i in [1, 3, 5, 10]:
        print(f'[TOP {i}]', topk_acc[i - 1])
