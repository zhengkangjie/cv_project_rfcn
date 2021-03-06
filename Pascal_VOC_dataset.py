import os
import cv2
import torch
import scipy
import random
import numpy as np
from PIL import Image
import xml.etree.ElementTree as ET
from skimage import transform as sktsf
from torchvision import transforms as tvtsf
from torch.utils.data import Dataset, DataLoader, TensorDataset

class Pascal_VOC_dataset(Dataset):
    def __init__(self, devkit_path, min_size = 600, max_size = 1000, max_objs = 5, dataset_list = ['2007_trainval']):
        self._devkit_path = devkit_path
        self._data_paths = [os.path.join(self._devkit_path, 'VOC' + dataset.split('_')[0]) for dataset in dataset_list]
        self.ids = []
        self.images = []
        for ind, data_path in enumerate(self._data_paths):
            id_list_file = os.path.join(data_path, 'ImageSets', 'Main', '{0}.txt'.format(dataset_list[ind].split('_')[1]))
            self.ids = self.ids + [os.path.join(data_path, 'Annotations', id_.strip()  + '.xml') for id_ in open(id_list_file)]
            self.images = self.images + [os.path.join(data_path, 'JPEGImages', id_.strip() + '.jpg') for id_ in open(id_list_file)]
        self._classes = ('__background__', # always index 0
                         'aeroplane', 'bicycle', 'bird', 'boat',
                         'bottle', 'bus', 'car', 'cat', 'chair',
                         'cow', 'diningtable', 'dog', 'horse',
                         'motorbike', 'person', 'pottedplant',
                         'sheep', 'sofa', 'train', 'tvmonitor')
        self._num_classes = len(self._classes)
        self._class_to_ind = dict(zip(self._classes, range(self._num_classes)))
        self.min_size = min_size
        self.max_size = max_size
        self.max_objs = max_objs

    def __len__(self):
        return len(self.ids)

    def _load_pascal_annotation(self, Annotations_file):
        """
        Load image and bounding boxes info from XML file in the PASCAL VOC
        format.
        """
        filename = Annotations_file
        tree = ET.parse(filename)
        objs = tree.findall('object')
        # if not self.config['use_diff']:
        #     # Exclude the samples labeled as difficult
        #     non_diff_objs = [
        #         obj for obj in objs if int(obj.find('difficult').text) == 0]
        #     # if len(non_diff_objs) != len(objs):
        #     #     print 'Removed {} difficult objects'.format(
        #     #         len(objs) - len(non_diff_objs))
        #     objs = non_diff_objs
        num_objs = len(objs)

        boxes = np.zeros((num_objs, 4), dtype=np.float32)
        gt_classes = np.zeros((num_objs), dtype=np.int32)
        overlaps = np.zeros((num_objs, self._num_classes), dtype=np.float32)
        # "Seg" area for pascal is just the box area
        seg_areas = np.zeros((num_objs), dtype=np.float32)
        ishards = np.zeros((num_objs), dtype=np.int32)

        # Load object bounding boxes into a data frame.
        for ix, obj in enumerate(objs):
            bbox = obj.find('bndbox')
            # Make pixel indexes 0-based
            x1 = float(bbox.find('xmin').text) + 1
            y1 = float(bbox.find('ymin').text) + 1
            x2 = float(bbox.find('xmax').text) - 1
            y2 = float(bbox.find('ymax').text) - 1

            diffc = obj.find('difficult')
            difficult = 0 if diffc == None else int(diffc.text)
            ishards[ix] = difficult

            cls = self._class_to_ind[obj.find('name').text.lower().strip()]
            boxes[ix, :] = [x1, y1, x2, y2]
            gt_classes[ix] = cls
            overlaps[ix, cls] = 1.0
            seg_areas[ix] = (x2 - x1 + 1) * (y2 - y1 + 1)

        # overlaps = scipy.sparse.csr_matrix(overlaps)

        return {'boxes': boxes,
                'gt_classes': gt_classes,
                'gt_ishard': ishards,
                'gt_overlaps': overlaps,
                'flipped': False,
                'seg_areas': seg_areas}

    def _prep_im_for_blob(self, im, pixel_means, target_size, max_size):
        """Mean subtract and scale an image for use in a blob."""
        im = im.astype(np.float32, copy=False)
        im -= pixel_means
        im_shape = im.shape
        im_size_min = np.min(im_shape[0:2])
        im_size_max = np.max(im_shape[0:2])
        im_scale = float(target_size) / float(im_size_min)
        # Prevent the biggest axis from being more than MAX_SIZE
        if np.round(im_scale * im_size_max) > max_size:
            im_scale = float(max_size) / float(im_size_max)
        im = cv2.resize(im, None, None, fx=im_scale, fy=im_scale,
                        interpolation=cv2.INTER_LINEAR)

        return im, im_scale

    def _im_list_to_blob(self, ims):
        """Convert a list of images into a network input.
        Assumes images are already prepared (means subtracted, BGR order, ...).
        """
        max_shape = np.array([im.shape for im in ims]).max(axis=0)
        num_images = len(ims)
        blob = np.zeros((num_images, max_shape[0], max_shape[1], 3),
                        dtype=np.float32)
        for i in range(num_images):
            im = ims[i]
            blob[i, 0:im.shape[0], 0:im.shape[1], :] = im

        return blob

    def _preprocess(self, img, min_size=600, max_size=1000):
        C, H, W = img.shape
        scale1 = min_size / min(H, W)
        scale2 = max_size / max(H, W)
        scale = min(scale1, scale2)
        img = img / 255.
        img = sktsf.resize(img, (C, H * scale, W * scale), mode='reflect',anti_aliasing=False)
        # both the longer and shorter should be less than
        # max_size and min_size
        normalize = tvtsf.Normalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225])
        img = normalize(torch.from_numpy(img))
        return img.numpy()

    def _resize_bbox(self, bbox, in_size, out_size):
        bbox = bbox.copy()
        y_scale = float(out_size[0]) / in_size[0]
        x_scale = float(out_size[1]) / in_size[1]
        bbox[:, 0] = y_scale * bbox[:, 0]
        bbox[:, 2] = y_scale * bbox[:, 2]
        bbox[:, 1] = x_scale * bbox[:, 1]
        bbox[:, 3] = x_scale * bbox[:, 3]
        return bbox

    def _flip_bbox(self, bbox, size, y_flip=False, x_flip=False):
        H, W = size
        bbox = bbox.copy()
        if y_flip:
            y_max = H - bbox[:, 0]
            y_min = H - bbox[:, 2]
            bbox[:, 0] = y_min
            bbox[:, 2] = y_max
        if x_flip:
            x_max = W - bbox[:, 1]
            x_min = W - bbox[:, 3]
            bbox[:, 1] = x_min
            bbox[:, 3] = x_max
        return bbox

    def _random_flip(self, img, y_random=False, x_random=False,
                    return_param=False, copy=False):
        y_flip, x_flip = False, False
        if y_random:
            y_flip = random.choice([True, False])
        if x_random:
            x_flip = random.choice([True, False])

        if y_flip:
            img = img[:, ::-1, :]
        if x_flip:
            img = img[:, :, ::-1]

        if copy:
            img = img.copy()

        if return_param:
            return img, {'y_flip': y_flip, 'x_flip': x_flip}
        else:
            return img

    def _transform(self, in_data):
        img, bbox, label = in_data
        _, H, W = img.shape
        img = self._preprocess(img, self.min_size, self.max_size)
        _, o_H, o_W = img.shape
        scale = o_H / H
        bbox = self._resize_bbox(bbox, (H, W), (o_H, o_W))

        # horizontally flip
        img, params = self._random_flip(
            img, x_random=True, return_param=True)
        bbox = self._flip_bbox(
            bbox, (o_H, o_W), x_flip=params['x_flip'])

        return img, bbox, label, scale

    def __getitem__(self, i):
        anno = self._load_pascal_annotation(self.ids[i])
        f = Image.open(self.images[i])
        try:
            img = f.convert('RGB')
            img = np.asarray(img, dtype=np.float32)
        finally:
            if hasattr(f, 'close'):
                f.close()

        if img.ndim == 2:
            # reshape (H, W) -> (1, H, W)
            img = img[np.newaxis]
        else:
            # transpose (H, W, C) -> (C, H, W)
            img = img.transpose((2, 0, 1))
        if anno['flipped']:
            img = img[:, ::-1, :]
        img = np.array(img).astype('float32')
        # print('img shape:', img.shape)
        img, bbox, label, scale = self._transform((img, anno['boxes'], anno['gt_classes']))
        img_padding = np.zeros((img.shape[0], self.max_size, self.max_size), dtype=np.float32)
        img_padding[:, 0:img.shape[1], 0:img.shape[2]] = img
        bbox_padding = np.zeros((self.max_objs, 4), dtype=np.float32) - 1
        bbox_padding[0:min(bbox.shape[0], self.max_objs), 0:bbox.shape[1]] = bbox[0:min(bbox.shape[0], self.max_objs),:]
        label_padding = np.zeros((self.max_objs, ), dtype=np.int32) - 1
        label_padding[0:min(label.shape[0], self.max_objs)] = label[0:min(label.shape[0], self.max_objs)]
        # print('img_padding shape:', img_padding.shape)
        # print('bbox_padding shape:', bbox_padding.shape)
        # print('label_padding shape:', label_padding.shape)
        # print('scale shape:', scale.shape)
        return img_padding.copy(), bbox_padding.copy(), label_padding.copy(), scale

        

