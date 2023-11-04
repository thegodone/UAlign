"""
Canonicalize the product SMILES, and then use substructure matching to infer
the correspondence to the original atom-mapped order. This correspondence is then
used to renumber the reactant atoms.
"""

import rdkit
import os
import argparse
import pandas as pd
from rdkit import Chem


def canonicalize_prod(p):
    import copy
    p = copy.deepcopy(p)
    p = canonicalize(p)
    p_mol = Chem.MolFromSmiles(p)
    for atom in p_mol.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx() + 1)
    p = Chem.MolToSmiles(p_mol)
    return p


def remove_amap_not_in_product(rxn_smi):
    """
    Corrects the atom map numbers of atoms only in reactants. 
    This correction helps avoid the issue of duplicate atom mapping
    after the canonicalization step.
    """
    r, p = rxn_smi.split(">>")

    pmol = Chem.MolFromSmiles(p)
    pmol_amaps = set([atom.GetAtomMapNum() for atom in pmol.GetAtoms()])
    # Atoms only in reactants are labelled starting with max_amap
    max_amap = max(pmol_amaps)

    rmol = Chem.MolFromSmiles(r)

    for atom in rmol.GetAtoms():
        amap_num = atom.GetAtomMapNum()
        if amap_num not in pmol_amaps:
            atom.SetAtomMapNum(max_amap + 1)
            max_amap += 1

    r_updated = Chem.MolToSmiles(rmol)
    rxn_smi_updated = r_updated + ">>" + p
    return rxn_smi_updated


def canonicalize(smiles):
    try:
        tmp = Chem.MolFromSmiles(smiles)
    except:
        print('no mol', flush=True)
        return smiles
    if tmp is None:
        return smiles
    tmp = Chem.RemoveHs(tmp)
    [a.ClearProp('molAtomMapNumber') for a in tmp.GetAtoms()]
    return Chem.MolToSmiles(tmp)


def infer_correspondence(p):
    orig_mol = Chem.MolFromSmiles(p)
    canon_mol = Chem.MolFromSmiles(canonicalize_prod(p))
    matches = list(canon_mol.GetSubstructMatches(orig_mol))[0]
    idx_amap = {
        atom.GetIdx(): atom.GetAtomMapNum()
        for atom in orig_mol.GetAtoms()
    }

    correspondence = {}
    for idx, match_idx in enumerate(matches):
        match_anum = canon_mol.GetAtomWithIdx(match_idx).GetAtomMapNum()
        old_anum = idx_amap[idx]
        correspondence[old_anum] = match_anum
    return correspondence


def remap_rxn_smi(rxn_smi):
    r, p = rxn_smi.split(">>")
    canon_mol = Chem.MolFromSmiles(canonicalize_prod(p))
    correspondence = infer_correspondence(p)

    rmol = Chem.MolFromSmiles(r)
    for atom in rmol.GetAtoms():
        atomnum = atom.GetAtomMapNum()
        if atomnum in correspondence:
            newatomnum = correspondence[atomnum]
            atom.SetAtomMapNum(newatomnum)

    rmol = Chem.MolFromSmiles(Chem.MolToSmiles(rmol))
    rxn_smi_new = Chem.MolToSmiles(rmol) + ">>" + Chem.MolToSmiles(canon_mol)
    return rxn_smi_new, correspondence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--filename", required=True,
        help="File with reactions to canonicalize"
    )
    args = parser.parse_args()

    file_path = os.path.abspath(args.filename)
    file_name = os.path.basename(file_path)
    file_dir = os.path.dirname(file_path)
    new_file = f"canonicalized_{file_name}"
    df = pd.read_csv(args.filename)
    print(f"Processing file of size: {len(df)}")

    new_dict = {'id': [], 'class': [], 'reactants>reagents>production': []}
    for idx in range(len(df)):
        element = df.loc[idx]
        uspto_id, class_id = element['id'], element['class']
        rxn_smi = element['reactants>reagents>production']

        rxn_smi_new = remove_amap_not_in_product(rxn_smi)
        rxn_smi_new, _ = remap_rxn_smi(rxn_smi_new)
        new_dict['id'].append(uspto_id)
        new_dict['class'].append(class_id)
        new_dict['reactants>reagents>production'].append(rxn_smi_new)

    new_df = pd.DataFrame.from_dict(new_dict)
    new_df.to_csv(f"{file_dir}/{new_file}", index=False)


if __name__ == "__main__":
    main()
