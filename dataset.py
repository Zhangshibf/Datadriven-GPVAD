import time
import torch
import numpy as np
import pandas as pd
import scipy
from h5py import File
import itertools, random
from tqdm import tqdm
from loguru import logger
import torch.utils.data as tdata
from typing import List, Dict


class TrainHDF5Dataset(tdata.Dataset):
    """
    HDF5 dataset indexed by a labels dataframe. 
    Indexing is done via the dataframe since we want to preserve some storage
    in cases where oversampling is needed ( pretty likely )
    """
    def __init__(self, h5filedict: Dict, h5labeldict: Dict, transform=None):
        super(TrainHDF5Dataset, self).__init__()
        self._h5filedict = h5filedict
        self._h5labeldict = h5labeldict
        self._len = len(self._h5labeldict)
        # IF none is passed still use no transform at all
        self._transform = transform
        self.idx_to_item = {
            idx: item
            for idx, item in enumerate(self._h5labeldict.keys())
        }
        first_item = next(iter(self._h5filedict.keys()))
        with File(self._h5filedict[first_item], 'r') as store:
            self.datadim = store[first_item].shape[-1]

    def __len__(self):
        return self._len

    def __getitem__(self, index):
        fname = self.idx_to_item[index]
        h5file = self._h5filedict[fname]
        labelh5file = self._h5labeldict[fname]
        with File(h5file, 'r') as datastore, File(labelh5file,
                                                  'r') as labelstore:
            data = datastore[fname][()]
            speech_target = labelstore[f"{fname}/speech"][()]
            noise_target = labelstore[f"{fname}/noise"][()]
            speech_clip_target = labelstore[f"{fname}/clipspeech"][()]
            noise_clip_target = labelstore[f"{fname}/clipnoise"][()]
        noise_clip_target = np.max(noise_clip_target)  # take max around axis
        target_clip = torch.tensor((noise_clip_target, speech_clip_target))
        data = torch.as_tensor(data).float()
        target_time = torch.as_tensor(
            np.stack((noise_target, speech_target), axis=-1)).float()
        if self._transform:
            data = self._transform(data)
        return data, target_time, target_clip, fname


class HDF5Dataset(tdata.Dataset):
    """
    HDF5 dataset indexed by a labels dataframe. 
    Indexing is done via the dataframe since we want to preserve some storage
    in cases where oversampling is needed ( pretty likely )
    """
    def __init__(self, h5file: File, h5label: File, fnames, transform=None):
        super(HDF5Dataset, self).__init__()
        self._h5file = h5file
        self._h5label = h5label
        self.fnames = fnames
        self.dataset = None
        self.label_dataset = None
        self._len = len(fnames)
        # IF none is passed still use no transform at all
        self._transform = transform
        with File(self._h5file, 'r') as store, File(self._h5label,
                                                    'r') as labelstore:
            self.datadim = store[self.fnames[0]].shape[-1]

    def __len__(self):
        return self._len

    def __getitem__(self, index):
        if self.dataset is None:
            self.dataset = File(self._h5file, 'r')
            self.label_dataset = File(self._h5label, 'r')
        fname = self.fnames[index]
        data = self.dataset[fname][()]
        speech_target = self.label_dataset[f"{fname}/speech"][()]
        noise_target = self.label_dataset[f"{fname}/noise"][()]
        speech_clip_target = self.label_dataset[f"{fname}/clipspeech"][()]
        noise_clip_target = self.label_dataset[f"{fname}/clipnoise"][()]
        noise_clip_target = np.max(noise_clip_target)  # take max around axis
        target_clip = torch.tensor((noise_clip_target, speech_clip_target))
        data = torch.as_tensor(data).float()
        target_time = torch.as_tensor(
            np.stack((noise_target, speech_target), axis=-1)).float()
        if self._transform:
            data = self._transform(data)
        return data, target_time, target_clip, fname


class EvalH5Dataset(tdata.Dataset):
    """
    HDF5 dataset indexed by a labels dataframe. 
    Indexing is done via the dataframe since we want to preserve some storage
    in cases where oversampling is needed ( pretty likely )
    """
    def __init__(self, h5file: File):
        super(EvalH5Dataset, self).__init__()
        self._h5file = h5file
        self._dataset = None
        # IF none is passed still use no transform at all
        with File(self._h5file, 'r') as store:
            self.fnames = list(store.keys())
            self.datadim = store[self.fnames[0]].shape[-1]
            self._len = len(store)

    def __len__(self):
        return self._len

    def __getitem__(self, index):
        if self._dataset is None:
            self._dataset = File(self._h5file, 'r')
        fname = self.fnames[index]
        data = self._dataset[fname][()]
        data = torch.as_tensor(data).float()
        return data, fname


class MinimumOccupancySampler(tdata.Sampler):
    """
        docstring for MinimumOccupancySampler
        samples at least one instance from each class sequentially
    """
    def __init__(self, labels, sampling_mode='same', random_state=None):
        self.labels = labels
        data_samples, n_labels = labels.shape
        label_to_idx_list, label_to_length = [], []
        self.random_state = np.random.RandomState(seed=random_state)
        for lb_idx in range(n_labels):
            label_selection = labels[:, lb_idx]
            if scipy.sparse.issparse(label_selection):
                label_selection = label_selection.toarray()
            label_indexes = np.where(label_selection == 1)[0]
            self.random_state.shuffle(label_indexes)
            label_to_length.append(len(label_indexes))
            label_to_idx_list.append(label_indexes)

        self.longest_seq = max(label_to_length)
        self.data_source = np.empty((self.longest_seq, len(label_to_length)),
                                    dtype=np.uint32)
        # Each column represents one "single instance per class" data piece
        for ix, leng in enumerate(label_to_length):
            # Fill first only "real" samples
            self.data_source[:leng, ix] = label_to_idx_list[ix]

        self.label_to_idx_list = label_to_idx_list
        self.label_to_length = label_to_length

        if sampling_mode == 'same':
            self.data_length = data_samples
        elif sampling_mode == 'over':  # Sample all items
            self.data_length = np.prod(self.data_source.shape)

    def _reshuffle(self):
        # Reshuffle
        for ix, leng in enumerate(self.label_to_length):
            leftover = self.longest_seq - leng
            random_idxs = np.random.randint(leng, size=leftover)
            self.data_source[leng:,
                             ix] = self.label_to_idx_list[ix][random_idxs]

    def __iter__(self):
        # Before each epoch, reshuffle random indicies
        self._reshuffle()
        n_samples = len(self.data_source)
        random_indices = self.random_state.permutation(n_samples)
        data = np.concatenate(
            self.data_source[random_indices])[:self.data_length]
        return iter(data)

    def __len__(self):
        return self.data_length


class MultiBalancedSampler(tdata.sampler.Sampler):
    """docstring for BalancedSampler
    Samples for Multi-label training
    Sampling is not totally equal, but aims to be roughtly equal
    """
    def __init__(self, Y, replacement=False, num_samples=None):
        assert Y.ndim == 2, "Y needs to be one hot encoded"
        if scipy.sparse.issparse(Y):
            raise ValueError("Not supporting sparse amtrices yet")
        class_counts = np.sum(Y, axis=0)
        class_weights = 1. / class_counts
        class_weights = class_weights / class_weights.sum()
        classes = np.arange(Y[0].shape[0])
        # Revert from many_hot to one
        class_ids = [tuple(classes.compress(idx)) for idx in Y]

        sample_weights = []
        for i in range(len(Y)):
            # Multiple classes were chosen, calculate average probability
            weight = class_weights[np.array(class_ids[i])]
            # Take the mean of the multiple classes and set as weight
            weight = np.mean(weight)
            sample_weights.append(weight)
        self._weights = torch.as_tensor(sample_weights, dtype=torch.float)
        self._len = num_samples if num_samples else len(Y)
        self._replacement = replacement

    def __len__(self):
        return self._len

    def __iter__(self):
        return iter(
            torch.multinomial(self._weights, self._len,
                              self._replacement).tolist())


def gettraindataloader(h5files, h5labels, transform=None, **dataloader_kwargs):
    dset = TrainHDF5Dataset(h5files, h5labels, transform=transform)

    return tdata.DataLoader(dset,
                            collate_fn=sequential_collate,
                            **dataloader_kwargs)


def getdataloader(h5file, h5label, fnames, transform=None,
                  **dataloader_kwargs):
    dset = HDF5Dataset(h5file, h5label, fnames, transform=transform)

    return tdata.DataLoader(dset,
                            collate_fn=sequential_collate,
                            **dataloader_kwargs)


def pad(tensorlist, padding_value=0.):
    lengths = [len(f) for f in tensorlist]
    max_len = np.max(lengths)
    # max_len = 2000
    batch_dim = len(lengths)
    data_dim = tensorlist[0].shape[-1]
    out_tensor = torch.full((batch_dim, max_len, data_dim),
                            fill_value=padding_value,
                            dtype=torch.float32)
    for i, tensor in enumerate(tensorlist):
        length = tensor.shape[0]
        out_tensor[i, :length, ...] = tensor[:length, ...]
    return out_tensor, torch.tensor(lengths)


def sequential_collate(batches):
    # sort length wise
    data, targets_time, targets_clip, fnames = zip(*batches)
    data, lengths_data = pad(data)
    targets_time, lengths_tar = pad(targets_time, padding_value=0)
    targets_clip = torch.stack(targets_clip)
    assert lengths_data.shape == lengths_tar.shape
    return data, targets_time, targets_clip, fnames, lengths_tar


if __name__ == '__main__':
    import utils
    labels_df = pd.read_csv('features/flists/unbalanced.csv',
                            sep='\t').convert_dtypes()
    # labels = pd.read_csv('features/flists/unbalanced_raw.csv', sep='\s+')

    label_array, encoder = utils.encode_labels(
        labels=labels_df['event_labels'])

    sampler = MinimumOccupancySampler(label_array)
    print(sampler)
    # labels['filename'] = labels['filename'].apply(os.path.basename)
    dloader = getdataloader(
        {
            'filename': labels_df['filename'].values,
            'encoded': label_array
        },
        'features/hdf5/unbalanced.h5',
        shuffle=False,
        batch_size=64,
        num_workers=4)

    for feat, target, fname in tqdm(dloader):
        print(target.shape)
        pass