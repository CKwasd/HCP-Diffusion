"""
bucket.py
====================
    :Name:        aspect ratio bucket with k-means
    :Author:      Dong Ziyi
    :Affiliation: HCP Lab, SYSU
    :Created:     10/03/2023
    :Licence:     Apache-2.0
"""

import os.path
import pickle

import numpy as np
import math
from sklearn.cluster import KMeans
from hcpdiff.utils.img_size_tool import types_support, get_image_size
from .utils import resize_crop_fix
from hcpdiff.utils.utils import get_file_ext

from typing import Tuple, Union
from loguru import logger

class BaseBucket:
    def __getitem__(self, idx):
        '''
        :return: (file name of image), (target image size)
        '''
        raise NotImplementedError()

    def __len__(self):
        raise NotImplementedError()

    def rest(self, epoch):
        pass

    def crop_resize(self, image, size):
        return image

class FixedBucket(BaseBucket):
    def __init__(self, img_root:str, target_size:Union[Tuple[int,int], int]=512):
        self.img_root=img_root
        self.target_size=(target_size, target_size) if isinstance(target_size, int) else target_size
        self.file_names=[x for x in os.listdir(img_root) if get_file_ext(x) in types_support]

    def crop_resize(self, image, size):
        return resize_crop_fix(image, size)

    def __getitem__(self, idx) -> Tuple[str, Tuple[int, int]]:
        return os.path.join(self.img_root, self.file_names[idx]), self.target_size

    def __len__(self):
        return len(self.file_names)

class RatioBucket(BaseBucket):
    def __init__(self, img_root:str, taget_area:int=640*640, step_size:int=8, num_bucket:int=10, pre_build_arb:str=None):
        self.img_root=img_root
        self.taget_area=taget_area
        self.step_size=step_size
        self.num_bucket=num_bucket
        self.pre_build_arb=pre_build_arb

        if pre_build_arb and os.path.exists(self.pre_build_arb):
            self.load_arb(pre_build_arb)
        else:
            self.file_names = [x for x in os.listdir(img_root) if get_file_ext(x) in types_support]

    def load_arb(self, path):
        with open(path, 'rb') as f:
            data = pickle.load(f)
        self.buckets=data['buckets']
        self.size_buckets=data['size_buckets']
        self.file_names=data['file_names']
        self.idx_bucket_map=data['idx_bucket_map']
        self.data_len = data['data_len']

    def save_arb(self, path):
        with open(path, 'wb') as f:
            pickle.dump({
                'buckets': self.buckets,
                'size_buckets': self.size_buckets,
                'idx_bucket_map': self.idx_bucket_map,
                'file_names': self.file_names,
                'data_len': self.data_len,
            }, f)

    def build_buckets_from_ratios(self, ratio_max:float=4):
        logger.info('build buckets from ratios')
        size_low = int(math.sqrt(self.taget_area / ratio_max))
        size_high = int(ratio_max*size_low)

        # SD需要边长是8的倍数
        size_low = (size_low//self.step_size)*self.step_size
        size_high = (size_high//self.step_size)*self.step_size

        data = []
        for w in range(size_low, size_high + 1, self.step_size):
            for h in range(size_low, size_high + 1, self.step_size):
                data.append([w * h, np.log2(w / h), w, h]) #对比例取对数，更符合人感知，宽高相反的可以对称分布。
        data = np.array(data)

        error_area = np.abs(data[:, 0] - self.taget_area)
        data_use = data[np.argsort(error_area)[:self.num_bucket*3], :] #取最小的num_bucket*3个

        #聚类，选出指定个数的bucket
        kmeans = KMeans(n_clusters=self.num_bucket, random_state=0).fit(data_use[:, 1].reshape(-1, 1))
        labels = kmeans.labels_
        self.buckets = [] # [bucket_id:[file_idx,...]]
        self.ratios_log = []
        self.size_buckets = []
        for i in range(self.num_bucket):
            map_idx = np.where(labels == i)[0]
            m_idx = map_idx[np.argmin(np.abs(data_use[labels == i, 1] - np.median(data_use[labels == i, 1])))]
            #self.buckets[wh_hash(*data_use[m_idx, 2:])]=[]
            self.buckets.append([])
            self.ratios_log.append(data_use[m_idx, 1])
            self.size_buckets.append(data_use[m_idx, 2:].astype(int))
        self.ratios_log=np.array(self.ratios_log)
        self.size_buckets=np.array(self.size_buckets)

        # fill buckets with images w,h
        self.idx_bucket_map=np.empty(len(self.file_names), dtype=int)
        for i, file in enumerate(self.file_names):
            file = os.path.join(self.img_root, file)
            w, h = get_image_size(file)
            bucket_id = np.abs(self.ratios_log-np.log2(w / h)).argmin()
            self.buckets[bucket_id].append(i)
            self.idx_bucket_map[i]=bucket_id
        logger.info('buckets info: ' + ', '.join(f'size:{self.size_buckets[i]}, num:{len(b)}' for i, b in enumerate(self.buckets)))

    def build_buckets_from_images(self):
        logger.info('build buckets from images')
        ratio_list = []
        for i, file in enumerate(self.file_names):
            file = os.path.join(self.img_root, file)
            w, h = get_image_size(file)
            ratio = np.log2(w / h)
            ratio_list.append(ratio)
        ratio_list=np.array(ratio_list)

        # 聚类，选出指定个数的bucket
        kmeans = KMeans(n_clusters=self.num_bucket, random_state=0).fit(ratio_list.reshape(-1, 1))
        labels = kmeans.labels_
        self.ratios_log = kmeans.cluster_centers_.reshape(-1)

        ratios=2**self.ratios_log
        h_all=np.sqrt(self.taget_area/ratios)
        w_all=h_all*ratios

        # SD需要边长是8的倍数
        h_all=(np.round(h_all / self.step_size) * self.step_size).astype(int)
        w_all=(np.round(w_all / self.step_size) * self.step_size).astype(int)
        self.size_buckets = list(zip(w_all, h_all))
        self.size_buckets = np.array(self.size_buckets)

        self.buckets = []  # [bucket_id:[file_idx,...]]
        self.idx_bucket_map = np.empty(len(self.file_names), dtype=int)
        for bidx in range(self.num_bucket):
            bnow = labels == bidx
            self.buckets.append(np.where(bnow)[0].tolist())
            self.idx_bucket_map[bnow]=bidx
        logger.info('buckets info: '+', '.join(f'size:{self.size_buckets[i]}, num:{len(b)}' for i, b in enumerate(self.buckets)))

    def make_arb(self, bs:int):
        '''
        :param bs: batch_size * n_gpus * accumulation_step
        :param pre_build_arb:
        '''
        self.bs = bs
        if self.pre_build_arb and os.path.exists(self.pre_build_arb):
            return

        rs = np.random.RandomState(42)
        # make len(bucket)%bs==0
        self.data_len = 0
        for bidx, bucket in enumerate(self.buckets):
            rest = len(bucket) % bs
            if rest > 0:
                bucket.extend(rs.choice(bucket, bs - rest))
            self.data_len += len(bucket)
            self.buckets[bidx]=np.array(bucket)

        if self.pre_build_arb:
            self.save_arb(self.pre_build_arb)

    def rest(self, epoch):
        rs = np.random.RandomState(42 + epoch)
        bucket_list = [x.copy() for x in self.buckets]
        # shuffle inter bucket
        for x in bucket_list:
            rs.shuffle(x)

        # shuffle of batches
        bucket_list = np.hstack(bucket_list).reshape(-1, self.bs)
        rs.shuffle(bucket_list)

        self.idx_arb = bucket_list.reshape(-1)

    def crop_resize(self, image, size):
        return resize_crop_fix(image, size)

    def __getitem__(self, idx):
        fidx = self.idx_arb[idx]
        bidx=self.idx_bucket_map[fidx]
        return os.path.join(self.img_root, self.file_names[fidx]), self.size_buckets[bidx]

    def __len__(self):
        return self.data_len

    @classmethod
    def from_ratios(cls, img_root:str, target_area:int=640*640, step_size:int=8, num_bucket:int=10, ratio_max:float=4,
                    pre_build_arb:str=None):
        arb = cls(img_root, target_area, step_size, num_bucket, pre_build_arb=pre_build_arb)
        arb.build_buckets_from_ratios(ratio_max)
        return arb

    @classmethod
    def from_files(cls, img_root:str, target_area:int=640*640, step_size:int=8, num_bucket:int=10, pre_build_arb:str=None):
        arb = RatioBucket(img_root, target_area, step_size, num_bucket, pre_build_arb=pre_build_arb)
        arb.build_buckets_from_images()
        return arb