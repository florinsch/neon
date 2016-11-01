# ----------------------------------------------------------------------------
# Copyright 2016 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
"""
Defines PASCAL_VOC datatset handling
"""
from __future__ import division
from builtins import zip, range

import numpy as np
import os
import xml.etree.ElementTree as ET
import tarfile
from PIL import Image
import abc

from generate_anchors import generate_all_anchors

from neon.data.datasets import Dataset
from neon.util.persist import save_obj, load_obj, get_data_cache_dir
from neon import logger as neon_logger

# From Caffe:
# Pixel mean values (BGR order) as a (1, 1, 3) array
# These are the values originally used for training VGG16
# __C.PIXEL_MEANS = np.array([[[102.9801, 115.9465, 122.7717]]])
FRCN_PIXEL_MEANS = np.array([[[102.9801, 115.9465, 122.7717]]])

# the loaded image will be (H, W, C) need to make it (C, H, W)
FRCN_IMG_DIM_SWAP = (2, 0, 1)

FRCN_EPS = 1.0

BB_XMIN_IDX = 0
BB_YMIN_IDX = 1
BB_XMAX_IDX = 2
BB_YMAX_IDX = 3

NORMALIZE_BBOX_TARGETS = False
BBOX_NORMALIZE_TARGETS_PRECOMPUTED = True  # True means the values below are used
BBOX_NORMALIZE_MEANS = [0.0, 0.0, 0.0, 0.0]  # taken from py-faster-rcnn caffe implementation
BBOX_NORMALIZE_STDS = [0.1, 0.1, 0.2, 0.2]

ASPECT_RATIO_GROUPING = True

DEBUG = False

dataset_meta = {
    'test-2007': dict(size=460032000,
                      file='VOCtest_06-Nov-2007.tar',
                      url='http://host.robots.ox.ac.uk/pascal/VOC/voc2007',
                      subdir='VOCdevkit/VOC2007'),
    'trainval-2007': dict(size=451020800,
                          file='VOCtrainval_06-Nov-2007.tar',
                          url='http://host.robots.ox.ac.uk/pascal/VOC/voc2007',
                          subdir='VOCdevkit/VOC2007'),
    'trainval-2012': dict(size=2000000000,
                          file='VOCtrainval_11-May-2012.tar',
                          url='http://host.robots.ox.ac.uk/pascal/VOC/voc2012',
                          subdir='VOCdevkit/VOC2012'),
}


class ObjectLocalization(Dataset):
    """
    Base class for loading object localization data in the PASCAL_VOC format.
    Data must include:
    1. index file of images
    2. XML file for each image

    Args:
        n_mb (int, optional): how many minibatch to iterate through, can use
                              value smaller than nbatches for debugging
        path (string, optional): path to the data directory.
        img_per_batch (int, optional): how many images processed per batch
        rpn_rois_per_img (int, optional): how many rois to pool from each image to train rpn
        frcn_rois_per_img (int, optional): how many rois to sample to train frcnn
        shuffle (boolean, optional): shuffle the image order during training
        rebuild_cache (boolean, optional): force the cache to be built from scratch
        subset_pct (float, optional): value between 0 and 100 indicating what percentage of the
                              dataset partition to use.  Defaults to 100.

    """
    MAX_SIZE = 1000
    MIN_SIZE = 600
    FRCNN_ROI_PER_IMAGE = 128  # number of rois to train (needed to initialize global buffers)
    RPN_ROI_PER_IMAGE = 256  # number of anchors per image
    IMG_PER_BATCH = 1  # number of images per batch
    CLASSES = None  # list of CLASSES e.g. ['__background__', 'car', 'people',..]
    SCALE = 1.0 / 16  # scaling factor of the image layers (e.g. VGG)

    # anchor variables
    RATIOS = [0.5, 1, 2]  # aspect ratios to generate
    SCALES = [128, 256, 512]  # box areas to generate

    NEGATIVE_OVERLAP = 0.3  # negative anchors have < 0.3 overlap with any gt box
    POSITIVE_OVERLAP = 0.7  # positive anchors have > 0.7 overlap with at least one gt box
    FG_FRACTION = 0.5  # at most, positive anchors are 0.5 of the total rois

    def __init__(self, path='.', n_mb=None, img_per_batch=None, conv_size=None,
                 rpn_rois_per_img=None, frcn_rois_per_img=None, add_flipped=False,
                 shuffle=False, deterministic=False, rebuild_cache=False, subset_pct=100,
                 mock_db=None):
        self.batch_index = 0
        self.path = path
        self.mock_db = mock_db

        # how many ROIs per image
        self.rois_per_img = rpn_rois_per_img if rpn_rois_per_img else self.RPN_ROI_PER_IMAGE
        self.img_per_batch = img_per_batch if img_per_batch else self.IMG_PER_BATCH
        self.rois_per_batch = self.rois_per_img * self.img_per_batch

        # how many ROIs to use to train frcnn
        self.frcn_rois_per_img = frcn_rois_per_img if frcn_rois_per_img \
            else self.FRCNN_ROI_PER_IMAGE

        assert self.img_per_batch == 1, "Only a minibatch of 1 is supported."

        self.num_classes = len(self.CLASSES)
        self._class_to_index = dict(list(zip(self.CLASSES, list(range(self.num_classes)))))

        # shape of the final conv layer
        if conv_size:
            self._conv_size = conv_size
        else:
            self._conv_size = int(np.floor(self.MAX_SIZE * self.SCALE))
        self._feat_stride = 1 / float(self.SCALE)
        self._num_scales = len(self.SCALES) * len(self.RATIOS)
        self._total_anchors = self._conv_size * self._conv_size * self._num_scales
        self.shuffle = shuffle
        self.deterministic = deterministic
        self.add_flipped = add_flipped

        # load the configure the dataset paths
        self.config = self.load_data()

        # annotation metadata
        self._annotation_file_ext = '.xml'
        self._annotation_obj_tag = 'object'
        self._annotation_class_tag = 'name'
        self._annotation_xmin_tag = 'xmin'
        self._annotation_xmax_tag = 'xmax'
        self._annotation_ymin_tag = 'ymin'
        self._annotation_ymax_tag = 'ymax'

        # self.rois_per_batch is 128 (2*64) ROIs
        # But the image path batch size is self.img_per_batch
        # need to control the batch size here
        assert self.img_per_batch is 1, "Only a batch size of 1 image is supported"

        neon_logger.display("Backend batchsize is changed to be {} "
                            "from Object Localization dataset".format(
                             self.img_per_batch))

        self.be.bsz = self.img_per_batch

        # 0. allocate buffers
        self.allocate()

        if not self.mock_db:
            # 1. read image index file
            assert os.path.exists(self.config['image_path']), \
                'Image index file does not exist: {}'.format(self.config['image_path'])
            with open(self.config['index_path']) as f:
                self.image_index = [x.strip() for x in f.readlines()]

            num_images = len(self.image_index)
            self.num_image_entries = num_images * 2 if self.add_flipped else num_images
            self.ndata = self.num_image_entries * self.rois_per_img
        else:
            self.num_image_entries = 1
            self.ndata = self.num_image_entries * self.rois_per_img

        assert (subset_pct > 0 and subset_pct <= 100), ('subset_pct must be between 0 and 100')

        if n_mb is not None:
            self.nbatches = n_mb
        else:
            self.nbatches = int(self.num_image_entries / self.img_per_batch * subset_pct / 100)

        self.cache_file = self.config['cache_path']

        if os.path.exists(self.cache_file) and not rebuild_cache and not self.mock_db:
            self.roi_db = load_obj(self.cache_file)
            neon_logger.display('ROI dataset loaded from file {}'.format(self.cache_file))

        elif not self.mock_db:
            # 2. read object Annotations (XML)
            roi_db = self.load_roi_groundtruth()

            if(self.add_flipped):
                roi_db = self.add_flipped_db(roi_db)

            # 3. construct acnhor targets
            self.roi_db = self.add_anchors(roi_db)

            if NORMALIZE_BBOX_TARGETS:
                # 4. normalize bbox targets by class
                self.roi_db = self.normalize_bbox_targets(self.roi_db)

            save_obj(self.roi_db, self.cache_file)
            neon_logger.display('wrote ROI dataset to {}'.format(self.cache_file))

        else:
            assert self.mock_db is not None
            roi_db = [self.mock_db]
            self.roi_db = self.add_anchors(roi_db)

        # 4. map anchors back to full canvas.
        # This is neccessary because the network outputs reflect the full canvas.
        # We cache the files in the unmapped state (above) to save memory.
        self.roi_db = unmap(self.roi_db)

    def allocate(self):

        # 1. allocate backend tensor for the image
        self.image_shape = (3, self.MAX_SIZE, self.MAX_SIZE)
        self.img_np = np.zeros(
            (3, self.MAX_SIZE, self.MAX_SIZE, self.be.bsz), dtype=np.float32)
        self.dev_X_img = self.be.iobuf(self.image_shape, dtype=np.float32)
        self.dev_X_img_chw = self.dev_X_img.reshape(
            3, self.MAX_SIZE, self.MAX_SIZE, self.be.bsz)

        self.shape = self.image_shape

        # For training, the RPN needs:
        # 1. bounding box target coordinates
        # 2. bounding box target masks (keep positive anchors only)
        self.dev_y_bbtargets = self.be.zeros((self._total_anchors * 4, 1))
        self.dev_y_bbtargets_mask = self.be.zeros((self._total_anchors * 4, 1))

        # 3. anchor labels of objectness
        # 4. objectness mask (ignore neutral anchors)
        self.dev_y_labels_flat = self.be.zeros((1, self._total_anchors), dtype=np.int32)
        self.dev_y_labels_onehot = self.be.zeros((2, self._total_anchors), dtype=np.int32)
        self.dev_y_labels = self.be.zeros((2 * self._total_anchors, 1), dtype=np.int32)

        self.dev_y_labels_mask = self.be.zeros((2 * self._total_anchors, 1), dtype=np.int32)

        # For training, Fast-RCNN needs:
        # 1. class labels
        # 2. bbox targets
        # The above are computed during fprop by the ProposalLayer,
        # so here we create the buffers to pass that to layer.
        self.dev_y_frcn_labels = self.be.zeros(
            (self.num_classes, self.frcn_rois_per_img), dtype=np.int32)
        self.dev_y_frcn_labels_mask = self.be.zeros(
            (self.num_classes, self.frcn_rois_per_img), dtype=np.int32)
        self.dev_y_frcn_bbtargets = self.be.zeros(
            (self.num_classes * 4, self.frcn_rois_per_img), dtype=np.float32)
        self.dev_y_frcn_bbmask = self.be.zeros(
            (self.num_classes * 4, self.frcn_rois_per_img), dtype=np.float32)

        # we also create some global buffers needed by the ProposalLayer
        # 1. image_shape
        # 2. gt_boxes
        # 3. number of gtboxes
        # 4. class label for each gt box
        # 5. image scale
        # 6. indexes of anchors actually generated for image out of 62x62 possible
        self.im_shape = self.be.zeros((2, 1), dtype=np.int32)
        self.gt_boxes = self.be.zeros((64, 4), dtype=np.float32)
        self.num_gt_boxes = self.be.zeros((1, 1), dtype=np.int32)
        self.gt_classes = self.be.zeros((64, 1), dtype=np.int32)
        self.im_scale = self.be.zeros((1, 1), dtype=np.float32)

    @abc.abstractmethod
    def load_data(self):
        """
        Abstract class to return a dictionary with data paths.

        The dictionary must contain:
        config['root'] # root directory of dataset
        config['index_path'] # index file with a list of images
        config['image_path'] # base directory for the images
        config['annot_path'] # base directory for the XML annotations
        config['file_ext'] # image file extension (e.g. *.jpg)
        """
        pass

    def get_global_buffers(self):

        global_buffers = dict()
        global_buffers['target_buffers'] = ((self.dev_y_frcn_labels, self.dev_y_frcn_labels_mask),
                                            (self.dev_y_frcn_bbtargets, self.dev_y_frcn_bbmask))
        global_buffers['img_info'] = (self.im_shape, self.im_scale)
        global_buffers['gt_boxes'] = (self.gt_boxes, self.gt_classes, self.num_gt_boxes)
        global_buffers['conv_config'] = (self._conv_size, self.SCALE)

        return global_buffers

    def add_anchors(self, roi_db):
        # adds a database of anchors

        # 1. for each i in (H,W), generate k=9 anchor boxes centered on i
        # 2. compute each anchor box against ground truth
        # 3. assign each anchor to positive (1), negative (0), or ignored (-1)
        # 4. for positive anchors, store the bbtargets

        # 1.
        # generate list of K anchor boxes, where K = # ratios * # scales
        # anchor boxes are coded as [xmin, ymin, xmax, ymax]
        all_anchors = generate_all_anchors(self._conv_size, self._conv_size, self.SCALE)
        # all_anchors are in (CHW) order, matching the CHWN output of the conv layer.
        assert self._total_anchors == all_anchors.shape[0]

        # 2.
        # Iterate through each image, and build list of positive/negative anchors
        for db in roi_db:

            im_scale, im_shape = self.calculate_scale_shape(db['img_shape'])

            # only keep anchors inside image
            idx_inside = inside_im_bounds(all_anchors, im_shape)

            if DEBUG:
                neon_logger.display('im shape', im_shape)
                neon_logger.display('idx inside', len(idx_inside))

            anchors = all_anchors[idx_inside, :]

            labels = np.empty((len(idx_inside), ), dtype=np.float32)
            labels.fill(-1)

            # compute bbox overlaps
            overlaps = calculate_bb_overlap(np.ascontiguousarray(anchors, dtype=np.float),
                                            np.ascontiguousarray(db['gt_bb'] * im_scale,
                                            dtype=np.float))

            # assign bg labels first
            bg_idx = overlaps.max(axis=1) < self.NEGATIVE_OVERLAP
            labels[bg_idx] = 0

            # assing fg labels

            # 1. for each gt box, anchor with higher overlaps [including ties]
            gt_idx = np.where(overlaps == overlaps.max(axis=0))[0]
            labels[gt_idx] = 1

            # 2. any anchor above the overlap threshold with any gt box
            fg_idx = overlaps.max(axis=1) >= self.POSITIVE_OVERLAP
            labels[fg_idx] = 1

            if DEBUG:
                neon_logger.display('max_overlap: {}'.format(overlaps.max()))
                neon_logger.display('Assigned {} bg labels'.format(bg_idx.sum()))
                neon_logger.display('Assigned {}+{} fg labels'.format(fg_idx.sum(), len(gt_idx)))
                neon_logger.display('Total fg labels: {}'.format(np.sum(labels == 1)))
                neon_logger.display('Total bg labels: {}'.format(np.sum(labels == 0)))

            # For every anchor, compute the regression target compared
            # to the gt box that it has the highest overlap with
            # the indicies of labels should match these targets
            bbox_targets = np.zeros((len(idx_inside), 4), dtype=np.float32)
            bbox_targets = _compute_targets(db['gt_bb'][overlaps.argmax(axis=1), :] * im_scale,
                                            anchors)

            # store class label of max_overlap gt to use in normalization
            gt_max_overlap_classes = overlaps.argmax(axis=1)

            # store results in database
            db['labels'] = labels
            db['bbox_targets'] = bbox_targets
            db['max_classes'] = gt_max_overlap_classes
            db['total_anchors'] = self._total_anchors
            db['idx_inside'] = idx_inside
            db['im_width'] = im_shape[0]
            db['im_height'] = im_shape[1]

        return roi_db

    def normalize_bbox_targets(self, roi_db):
        if BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
            # Use fixed / precomputed "means" and "stds" instead of empirical values
            means = np.tile(
                np.array(BBOX_NORMALIZE_MEANS), (self.num_classes, 1))
            stds = np.tile(
                np.array(BBOX_NORMALIZE_STDS), (self.num_classes, 1))

        else:
            # Compute values needed for means and stds
            # var(x) = E(x^2) - E(x)^2
            # Add epsilon for classes with 0 counts
            class_counts = np.zeros((self.num_classes, 1)) + 1e-14
            sums = np.zeros((self.num_classes, 4))
            squared_sums = np.zeros((self.num_classes, 4))
            for im_i in (self.num_image_entries):
                targets = roi_db[im_i]['bbox_targets']
                for cls in range(1, self.num_classes):
                    cls_inds = np.where(roi_db[im_i]['gt_classes'] == cls)[0]
                    if cls_inds.size > 0:
                        class_counts[cls] += cls_inds.size
                        sums[cls, :] += targets[cls_inds].sum(axis=0)
                        squared_sums[cls, :] += \
                            (targets[cls_inds] ** 2).sum(axis=0)

            means = sums / class_counts
            stds = np.sqrt(squared_sums / class_counts - means ** 2)

        if DEBUG:
            neon_logger.display('bbox target means:')
            neon_logger.display(means)
            neon_logger.display(means[1:, :].mean(axis=0))  # ignore bg class
            neon_logger.display('bbox target stdevs:')
            neon_logger.display(stds)
            neon_logger.display(stds[1:, :].mean(axis=0))  # ignore bg class

        # Normalize targets
        neon_logger.display("Normalizing targets")
        for im_i in range(self.num_image_entries):
            targets = roi_db[im_i]['bbox_targets']

            for cls in range(1, self.num_classes):
                cls_inds = np.where(roi_db[im_i]['max_classes'] == cls)[0]
                roi_db[im_i]['bbox_targets'][cls_inds] -= means[cls, :]
                roi_db[im_i]['bbox_targets'][cls_inds] /= stds[cls, :]

        return roi_db

    def load_roi_groundtruth(self):
        """
        load the voc database ground truth ROIs
        """
        return [self.load_annotation(img) for img in self.image_index]

    def load_annotation(self, image_index):
        """
        For a particular image, load ground truth annotations of object classes
        and their bounding rp from the pascal voc dataset files are in the
        VOC directory/Annotations. Each xml file corresponds to a particular
        image index
        """
        annotation_file = os.path.join(self.config['annot_path'],
                                       image_index + self._annotation_file_ext)

        tree = ET.parse(annotation_file)
        objs = tree.findall('object')
        if not self.config['use_diff']:
            # Exclude the samples labeled as difficult
            non_diff_objs = [
                obj for obj in objs if int(obj.find('difficult').text) == 0]
            objs = non_diff_objs
        num_objs = len(objs)

        # initialize ground truth classes and bb
        gt_bb = np.zeros((num_objs, 4), dtype=np.uint16)
        gt_classes = np.zeros((num_objs, 1), dtype=np.int32)

        # load all the info
        for idx, obj in enumerate(objs):
            bbox = obj.find('bndbox')
            # Make pixel indexes 0-based
            x1 = float(bbox.find(self._annotation_xmin_tag).text) - 1
            y1 = float(bbox.find(self._annotation_ymin_tag).text) - 1
            x2 = float(bbox.find(self._annotation_xmax_tag).text) - 1
            y2 = float(bbox.find(self._annotation_ymax_tag).text) - 1
            cls_label = self._class_to_index[obj.find('name').text.lower().strip()]

            gt_bb[idx] = [x1, y1, x2, y2]
            gt_classes[idx] = cls_label

        # load image and store shape
        img_path = os.path.join(self.config['image_path'],
                                image_index + self.config['file_ext'])

        im = Image.open(img_path)  # This is RGB order

        return {'gt_bb': gt_bb,
                'gt_classes': gt_classes,
                'img_id': image_index,
                'flipped': False,
                'img_path': img_path,
                'img_shape': im.size
                }

    def add_flipped_db(self, roi_db):
        roi_db_flipped = [None] * len(roi_db)

        for k, db in enumerate(roi_db):

            bb = db['gt_bb'].copy()
            width = db['img_shape'][0]

            bb[:, BB_XMIN_IDX] = width - \
                db['gt_bb'][:, BB_XMAX_IDX] - 1
            bb[:, BB_XMAX_IDX] = width - \
                db['gt_bb'][:, BB_XMIN_IDX] - 1

            roi_db_flipped[k] = {'gt_bb': bb,
                                 'flipped': True,
                                 'gt_classes': db['gt_classes'],
                                 'img_path': db['img_path'],
                                 'img_shape': db['img_shape'],
                                 'img_id': db['img_id'],
                                 }

        roi_db = roi_db + roi_db_flipped
        return roi_db

    def calculate_scale_shape(self, size):
        im_shape = np.array(size, np.int32)
        im_size_min = np.min(im_shape)
        im_size_max = np.max(im_shape)
        im_scale = float(self.MIN_SIZE) / float(im_size_min)
        # Prevent the biggest axis from being more than FRCN_MAX_SIZE
        if np.round(im_scale * im_size_max) > self.MAX_SIZE:
            im_scale = float(self.MAX_SIZE) / float(im_size_max)
        im_shape = np.round((im_shape * im_scale)).astype(int)
        return im_scale, im_shape

    def reset(self):
        """
        For resetting the starting index of this dataset back to zero.
        """
        self.batch_index = 0

    def _sample_anchors(self, db, nrois, fg_fractions, deterministic=False):

        # subsample labels if needed
        num_fg = int(fg_fractions * nrois)
        fg_idx = np.where(db['labels'] == 1)[0]
        bg_idx = np.where(db['labels'] == 0)[0]

        num_fg_this_img = min(num_fg, len(fg_idx))
        num_bg_this_img = min(nrois - num_fg_this_img, len(bg_idx))

        if not deterministic:
            fg_idx = self.be.rng.choice(fg_idx, size=num_fg_this_img, replace=False)
            bg_idx = self.be.rng.choice(bg_idx, size=num_bg_this_img, replace=False)
        else:
            fg_idx = fg_idx[:num_fg_this_img]
            bg_idx = bg_idx[:num_bg_this_img]

        idx = np.hstack([fg_idx, bg_idx])
        assert len(idx) == nrois

        # return labels, bbox_targets, and anchor indicies
        return (db['labels'][idx], db['bbox_targets'][idx, :], idx[:])

    def _shuffle_roidb(self):
        """
         Shuffle the indicies but grouping training examples in pairs
         based on the aspect ratio. Each image is categorized as either
         Horizontal (H) or Vertical (V) aspect ratio, and the shuffled image order
         is designed as [H, H, V, V, H, H, H, H, V, V, V, V] (e.g. pairs of images
         with the same aspect ratio are grouped together).
        """
        widths = np.array([r['im_width'] for r in self.roi_db])
        heights = np.array([r['im_height'] for r in self.roi_db])
        horz = (widths >= heights)
        vert = np.logical_not(horz)
        horz_inds = np.where(horz)[0]
        vert_inds = np.where(vert)[0]
        inds = np.hstack((
                         self.be.rng.permutation(horz_inds),
                         self.be.rng.permutation(vert_inds)))
        inds = np.reshape(inds, (-1, 2))
        row_perm = self.be.rng.permutation(np.arange(inds.shape[0]))
        inds = np.reshape(inds[row_perm, :], (-1,))

        return inds

    def __iter__(self):
        """
        Generator that can be used to iterate over this dataset.

        Yields:
            X: image data in CHW order and BGR color order
            Y: targets for the RPN, organized as:
               ((labels, labels_mask)), (bbox_targets, bbox_targets_mask))
        """
        self.batch_index = 0

        # each minibatch:
        # pass a single image
        # pass labels (256, )
        # pass bbox_targets (256, 4)
        # pass slice index (256, )

        # permute the dataset each epoch
        if self.shuffle is False:
            shuf_idx = list(range(self.num_image_entries))
        else:
            if ASPECT_RATIO_GROUPING:
                shuf_idx = self._shuffle_roidb()
            else:
                shuf_idx = self.be.rng.permutation(self.num_image_entries)

        assert self.img_per_batch == 1, "Only batch size of 1 supported."

        # TODO: move these into the minibatch loop
        bbtargets = np.zeros((self._total_anchors, 4), dtype=np.float32)
        bbtargets_mask = np.zeros((self._total_anchors, 4), dtype=np.int32)
        label = np.zeros((self._total_anchors, 1), dtype=np.float32)
        label_mask = np.zeros((self._total_anchors, 1), dtype=np.int32)

        for self.batch_index in range(self.nbatches):

            db = self.roi_db[shuf_idx[self.batch_index]]

            self.img_np[:] = 0

            if not self.mock_db:
                # load and process the image using PIL
                im = Image.open(db['img_path'])  # This is RGB order
            else:
                im = Image.new('RGB', (db['img_shape'][0], db['img_shape'][1]))
            im_scale, im_shape = self.calculate_scale_shape(im.size)

            # store metadata in buffer for ProposalLayer
            self.im_shape.set(im_shape)
            self.im_scale.set(np.array(im_scale))
            num_gt_boxes = np.array(db['gt_bb'].shape[0])
            self.num_gt_boxes.set(num_gt_boxes)
            self.gt_boxes[:num_gt_boxes, :] = db['gt_bb'] * im_scale
            self.gt_classes[:num_gt_boxes] = db['gt_classes']

            im = im.resize(im_shape, Image.LINEAR)

            # load image to numpy and flip the channel RGB to BGR
            im = np.array(im)[:, :, ::-1]

            # flip image if needed
            if db['flipped']:
                im = im[:, ::-1, :]

            # Mean subtract and scale an image
            im = im.astype(np.float32, copy=False)

            im -= FRCN_PIXEL_MEANS

            # sample anchors to use as targets
            labels, bbox_targets, anchor_index = self._sample_anchors(db, self.rois_per_img,
                                                                      self.FG_FRACTION,
                                                                      self.deterministic)

            # write image to backend tensor
            self.img_np[:, :im_shape[1], :im_shape[0], 0] = im.transpose(FRCN_IMG_DIM_SWAP)

            # copy to backend tensors
            self.dev_X_img_chw.set(self.img_np)

            # map our labels and bbox_targets back to
            # the full canvas (e.g. 9 * (62 * 62))
            label.fill(0)
            label[anchor_index, :] = labels[:, np.newaxis]
            self.dev_y_labels_flat[:] = label.reshape((1, -1))
            self.dev_y_labels_onehot[:] = self.be.onehot(self.dev_y_labels_flat, axis=0)
            self.dev_y_labels = self.dev_y_labels_onehot.reshape((-1, 1))

            label_mask.fill(0)
            label_mask[anchor_index, :] = 1
            self.dev_y_labels_mask[:] = np.vstack([label_mask, label_mask])

            bbtargets.fill(0)
            bbtargets[anchor_index, :] = bbox_targets
            # Try not using the sample targets but instead use the full targets
            # (the mask already has the sample info, and this is how caffe does it)
            # self.dev_y_bbtargets[:] =  bbtargets.T.reshape((-1, 1))
            self.dev_y_bbtargets[:] = db['bbox_targets'].T.reshape((-1, 1))

            bbtargets_mask.fill(0)
            bbtargets_mask[np.where(label == 1)[0]] = 1
            self.dev_y_bbtargets_mask[:] = bbtargets_mask.T.reshape((-1, 1))

            X = self.dev_X_img
            Y = ((self.dev_y_labels, self.dev_y_labels_mask),
                 (self.dev_y_bbtargets, self.dev_y_bbtargets_mask),
                 ((self.dev_y_frcn_labels, self.dev_y_frcn_labels_mask),
                  (self.dev_y_frcn_bbtargets, self.dev_y_frcn_bbmask)))

            yield X, Y

    def evaluation(self, all_boxes, output_dir='output'):
        """
        Evaluations on all detections which are collected into:
        all_boxes[cls][image] = N x 5 array of detections in (x1, y1, x2, y2, score).
        It will write outputs into text format.
        Then call voc_eval function outside of this step to generate mAP metric
        using the text files.

        Arguments:
            all_boxes (ndarray): detections over all classes and all images
            output_dir (str): where to save the output files
        """
        neon_logger.display('--------------------------------------------------------------')
        neon_logger.display('Computing results with **unofficial** Python eval code.')
        neon_logger.display('--------------------------------------------------------------')

        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)

        for cls_ind, cls in enumerate(self.CLASSES):
            if cls == '__background__':
                continue
            neon_logger.display('Writing {} VOC results file'.format(cls))
            filename = 'voc_{}_{}_{}.txt'.format(
                self.year, self.image_set, cls)
            filepath = os.path.join(output_dir, filename)
            with open(filepath, 'wt') as f:
                for im_ind in range(self.nbatches):
                    index = self.image_index[im_ind]
                    dets = all_boxes[im_ind][cls_ind]
                    if dets == []:
                        continue
                    # the VOCdevkit expects 1-based indices
                    for k in range(dets.shape[0]):
                        f.write('{:s} {:.3f} {:.1f} {:.1f} {:.1f} {:.1f}\n'.
                                format(index, dets[k, -1],
                                       dets[k, 0] + 1, dets[k, 1] + 1,
                                       dets[k, 2] + 1, dets[k, 3] + 1))

        annopath = os.path.join(self.config['annot_path'],
                                '{:s}' + self._annotation_file_ext)
        imagesetfile = self.config['index_path']

        return annopath, imagesetfile


class PASCAL(ObjectLocalization):
    MAX_SIZE = 1000  # 1000 # the max image scales on the max dim
    MIN_SIZE = 600  # 600 # the max image scales on the min dim
    ROI_PER_IMAGE = 256
    IMG_PER_BATCH = 1
    SCALE = 1.0 / 16
    NUM_SCALES = 9

    # background class is always indexed at 0
    CLASSES = ('__background__',
               'aeroplane', 'bicycle', 'bird', 'boat',
               'bottle', 'bus', 'car', 'cat', 'chair',
               'cow', 'diningtable', 'dog', 'horse',
               'motorbike', 'person', 'pottedplant',
               'sheep', 'sofa', 'train', 'tvmonitor')

    def __init__(self, image_set, year, path='.', n_mb=None, img_per_batch=None,
                 conv_size=None, rpn_rois_per_img=None, frcn_rois_per_img=None, add_flipped=True,
                 shuffle=True, deterministic=False, rebuild_cache=False, subset_pct=100,
                 mock_db=None):

        self.image_set = image_set
        self.year = year
        super(PASCAL, self).__init__(path, n_mb, img_per_batch, conv_size,
                                     rpn_rois_per_img, frcn_rois_per_img, add_flipped,
                                     shuffle, deterministic, rebuild_cache, subset_pct, mock_db)

    def load_data(self):
        """
        """
        dataset = '-'.join([self.image_set, self.year])

        voc = dataset_meta[dataset]
        workdir, filepath, datadir = self._valid_path_append(
            self.path, '', voc['file'], voc['subdir'])

        if not os.path.exists(filepath):
            self.fetch_dataset(voc['url'], voc['file'], filepath, voc['size'])
            with tarfile.open(filepath) as f:
                f.extractall(workdir)

        # define the path structure of the dataset
        config = dict()
        config['root'] = datadir
        config['index_path'] = os.path.join(datadir, 'ImageSets', 'Main',
                                            self.image_set + '.txt')
        config['image_path'] = os.path.join(datadir, 'JPEGImages')
        config['annot_path'] = os.path.join(datadir, 'Annotations')
        config['file_ext'] = ".jpg"

        # write cache name
        cache_name = 'pascal_{}-{}.pkl'.format(self.image_set, self.year,
                                               self.MAX_SIZE, self.MIN_SIZE)

        cache_dir = get_data_cache_dir(datadir, subdir='pascalvoc_cache')
        config['cache_path'] = os.path.join(cache_dir, cache_name)
        config['use_diff'] = False

        return config


# begin utility functions

def load_data_from_xml_tag(element, tag):
    """
    Helper function for loading data with specfic tag
    """
    return element.getElementsByTagName(tag)[0].childNodes[0].data


def _compute_targets(gt_bb, rp_bb):
    """
    Given ground truth bounding boxes and proposed boxes, compute the regresssion
    targets according to:

    t_x = (x_gt - x) / w
    t_y = (y_gt - y) / h
    t_w = log(w_gt / w)
    t_h = log(h_gt / h)

    where (x,y) are bounding box centers and (w,h) are the box dimensions
    """
    # calculate the region proposal centers and width/height
    (x, y, w, h) = _get_xywh(rp_bb)
    (x_gt, y_gt, w_gt, h_gt) = _get_xywh(gt_bb)

    # the target will be how to adjust the bbox's center and width/height
    # note that the targets are generated based on the original RP, which has not
    # been scaled by the image resizing
    targets_dx = (x_gt - x) / w
    targets_dy = (y_gt - y) / h
    targets_dw = np.log(w_gt / w)
    targets_dh = np.log(h_gt / h)

    targets = np.concatenate((targets_dx[:, np.newaxis],
                              targets_dy[:, np.newaxis],
                              targets_dw[:, np.newaxis],
                              targets_dh[:, np.newaxis],
                              ), axis=1)
    return targets


def _get_xywh(bb):
    """
    Given bounding boxes with coordinates (x_min, y_min, x_max, y_max), transform to
    (x_center, y_center, width, height)
    """
    w = bb[:, BB_XMAX_IDX] - bb[:, BB_XMIN_IDX] + FRCN_EPS
    h = bb[:, BB_YMAX_IDX] - bb[:, BB_YMIN_IDX] + FRCN_EPS
    x = bb[:, BB_XMIN_IDX] + 0.5 * w
    y = bb[:, BB_YMIN_IDX] + 0.5 * h

    return (x, y, w, h)


def unmap(roi_db):
    """
    For each entry in a database, unmap the labels and bounding box targets
    back to the full canvas size.
    """
    for db in roi_db:
        db['labels'] = _unmap(db['labels'], db['total_anchors'], db['idx_inside'], fill=-1)

        db['bbox_targets'] = _unmap(db['bbox_targets'], db['total_anchors'],
                                    db['idx_inside'], fill=0)

        db['max_classes'] = _unmap(db['max_classes'], db['total_anchors'],
                                   db['idx_inside'], fill=0)

    return roi_db


def _unmap(data, count, inds, fill=0):
    """
    Unmap a subset of item (data) back to the original set of items (of
    size count). From Girshick et al.
    """
    if len(data.shape) == 1:
        ret = np.empty((count, ), dtype=np.float32)
        ret.fill(fill)
        ret[inds] = data
    else:
        ret = np.empty((count, ) + data.shape[1:], dtype=np.float32)
        ret.fill(fill)
        ret[inds, :] = data
    return ret


def inside_im_bounds(box, im_shape):
    """
    Returns true if the box is inside the image's boundaries. Coordinates
    must be in (x_min, y_min, x_max, y_max) format.
    """
    return np.where(
        (box[:, 0] >= 0) &
        (box[:, 1] >= 0) &
        (box[:, 2] < im_shape[0]) &
        (box[:, 3] < im_shape[1])
    )[0]


def calculate_bb_overlap(rp, gt):
    """
    Returns a matrix of overlaps between every possible pair of the two provided
    bounding box lists.

    Arguments:
        rp (list): an array of region proposals, shape (R, 4)
        gt (list): an array of ground truth ROIs, shape (G, 4)

    Outputs:
        overlaps: a matrix of overlaps between 2 list, shape (R, G)
    """
    R = rp.shape[0]
    G = gt.shape[0]
    overlaps = np.zeros((R, G), dtype=np.float32)

    for g in range(G):
        gt_box_area = float(
            (gt[g, 2] - gt[g, 0] + 1) *
            (gt[g, 3] - gt[g, 1] + 1)
        )
        for r in range(R):
            iw = float(
                min(rp[r, 2], gt[g, 2]) -
                max(rp[r, 0], gt[g, 0]) + 1
            )
            if iw > 0:
                ih = float(
                    min(rp[r, 3], gt[g, 3]) -
                    max(rp[r, 1], gt[g, 1]) + 1
                )
                if ih > 0:
                    ua = float(
                        (rp[r, 2] - rp[r, 0] + 1) *
                        (rp[r, 3] - rp[r, 1] + 1) +
                        gt_box_area - iw * ih
                    )
                    overlaps[r, g] = iw * ih / ua
    return overlaps
