# Written by jbwang1997
# Reference: https://github.com/jbwang1997/BboxToolkit

import argparse
import codecs
import datetime
import itertools
import json
import logging
import os
import os.path as osp
import time
from functools import partial, reduce
from math import ceil
from multiprocessing import Manager, Pool

import cv2
import numpy as np
from PIL import Image

try:
    import shapely.geometry as shgeo
except ImportError:
    shgeo = None


def add_parser(parser):
    # argument for processing
    parser.add_argument(
        '--base_json',
        type=str,
        default=None,
        help='json config file for split images')
    parser.add_argument(
        '--nproc', type=int, default=10, help='the procession number')

    # argument for loading data
    parser.add_argument(
        '--load_type', type=str, default=None, help='loading function type')
    parser.add_argument(
        '--img_dirs',
        nargs='+',
        type=str,
        default=None,
        help='images dirs, must give a value')
    parser.add_argument(
        '--ann_dirs',
        nargs='+',
        type=str,
        default=None,
        help='annotations dirs, optional')
    parser.add_argument(
        '--classes',
        nargs='+',
        type=str,
        default=None,
        help='the classes and order for loading data')

    # argument for splitting image
    parser.add_argument(
        '--sizes',
        nargs='+',
        type=int,
        default=[1024],
        help='the sizes of sliding windows')
    parser.add_argument(
        '--gaps',
        nargs='+',
        type=int,
        default=[512],
        help='the steps of sliding widnows')
    parser.add_argument(
        '--rates',
        nargs='+',
        type=float,
        default=[1.],
        help='same as DOTA devkit rate, but only change windows size')
    parser.add_argument(
        '--img_rate_thr',
        type=float,
        default=0.6,
        help='the minimal rate of image in window and window')
    parser.add_argument(
        '--iof_thr',
        type=float,
        default=0.7,
        help='the minimal iof between a object and a window')
    parser.add_argument(
        '--no_padding',
        action='store_true',
        help='not padding patches in regular size')
    parser.add_argument(
        '--padding_value',
        nargs='+',
        type=int,
        default=[0],
        help='padding value, 1 or channel number')

    # argument for saving
    parser.add_argument(
        '--save_dir',
        type=str,
        default='.',
        help='to save pkl and split images')
    parser.add_argument(
        '--save_ext',
        type=str,
        default='.png',
        help='the extension of saving images')


def parse_args():
    parser = argparse.ArgumentParser(description='Splitting images')
    add_parser(parser)
    args = parser.parse_args()

    if args.base_json is not None:
        with open(args.base_json, 'r') as f:
            prior_config = json.load(f)

        for action in parser._actions:
            if action.dest not in prior_config or \
                    not hasattr(action, 'default'):
                continue
            action.default = prior_config[action.dest]
            args = parser.parse_args()

    # assert arguments
    assert args.load_type is not None, "argument load_type can't be None"
    assert args.img_dirs is not None, "argument img_dirs can't be None"
    assert args.ann_dirs is None or len(args.ann_dirs) == len(args.img_dirs)
    assert len(args.sizes) == len(args.gaps)
    assert len(args.sizes) == 1 or len(args.rates) == 1
    assert args.save_ext in ['.png', '.jpg', 'bmp', '.tif']
    assert args.iof_thr >= 0 and args.iof_thr < 1
    assert args.iof_thr >= 0 and args.iof_thr <= 1
    assert not osp.exists(args.save_dir), \
        f'{osp.join(args.save_dir)} already exists'
    return args


def get_sliding_window(info, sizes, gaps, img_rate_thr):
    eps = 0.01
    windows = []
    width, height = info['width'], info['height']
    for size, gap in zip(sizes, gaps):
        assert size > gap, f'invaild size gap pair [{size} {gap}]'
        step = size - gap

        x_num = 1 if width <= size else ceil((width - size) / step + 1)
        x_start = [step * i for i in range(x_num)]
        if len(x_start) > 1 and x_start[-1] + size > width:
            x_start[-1] = width - size

        y_num = 1 if height <= size else ceil((height - size) / step + 1)
        y_start = [step * i for i in range(y_num)]
        if len(y_start) > 1 and y_start[-1] + size > height:
            y_start[-1] = height - size

        start = np.array(
            list(itertools.product(x_start, y_start)), dtype=np.int64)
        stop = start + size
        windows.append(np.concatenate([start, stop], axis=1))
    windows = np.concatenate(windows, axis=0)

    img_in_wins = windows.copy()
    img_in_wins[:, 0::2] = np.clip(img_in_wins[:, 0::2], 0, width)
    img_in_wins[:, 1::2] = np.clip(img_in_wins[:, 1::2], 0, height)
    img_areas = (img_in_wins[:, 2] - img_in_wins[:, 0]) * \
                (img_in_wins[:, 3] - img_in_wins[:, 1])
    win_areas = (windows[:, 2] - windows[:, 0]) * \
                (windows[:, 3] - windows[:, 1])
    img_rates = img_areas / win_areas
    if not (img_rates > img_rate_thr).any():
        max_rate = img_rates.max()
        img_rates[abs(img_rates - max_rate) < eps] = 1
    return windows[img_rates > img_rate_thr]


def poly2hbb(polys):
    """Convert polygons to horizontal bboxes."""
    shape = polys.shape
    polys = polys.reshape(*shape[:-1], shape[-1] // 2, 2)
    lt_point = np.min(polys, axis=-2)
    rb_point = np.max(polys, axis=-2)
    return np.concatenate([lt_point, rb_point], axis=-1)


def bbox_overlaps_iof(bboxes1, bboxes2, eps=1e-6):
    rows = bboxes1.shape[0]
    cols = bboxes2.shape[0]

    if rows * cols == 0:
        return np.zeros((rows, cols), dtype=np.float32)

    hbboxes1 = poly2hbb(bboxes1)
    hbboxes2 = bboxes2
    hbboxes1 = hbboxes1[:, None, :]
    lt = np.maximum(hbboxes1[..., :2], hbboxes2[..., :2])
    rb = np.minimum(hbboxes1[..., 2:], hbboxes2[..., 2:])
    wh = np.clip(rb - lt, 0, np.inf)
    h_overlaps = wh[..., 0] * wh[..., 1]

    l, t, r, b = [bboxes2[..., i] for i in range(4)]
    polys2 = np.stack([l, t, r, t, r, b, l, b], axis=-1)
    if shgeo is None:
        raise ImportError('Please run "pip install shapely" '
                          'to install shapely first.')
    sg_polys1 = [shgeo.Polygon(p) for p in bboxes1.reshape(rows, -1, 2)]
    sg_polys2 = [shgeo.Polygon(p) for p in polys2.reshape(cols, -1, 2)]
    overlaps = np.zeros(h_overlaps.shape)
    for p in zip(*np.nonzero(h_overlaps)):
        overlaps[p] = sg_polys1[p[0]].intersection(sg_polys2[p[-1]]).area
    unions = np.array([p.area for p in sg_polys1], dtype=np.float32)
    unions = unions[..., None]

    unions = np.clip(unions, eps, np.inf)
    outputs = overlaps / unions
    if outputs.ndim == 1:
        outputs = outputs[..., None]
    return outputs


def get_window_obj(info, windows, iof_thr):
    bboxes = info['ann']['bboxes']
    iofs = bbox_overlaps_iof(bboxes, windows)  # 计算旋转框和划窗的iou

    window_anns = []
    for i in range(windows.shape[0]):
        win_iofs = iofs[:, i]
        pos_inds = np.nonzero(win_iofs >= iof_thr)[0].tolist()

        win_ann = dict()
        for k, v in info['ann'].items():
            try:
                win_ann[k] = v[pos_inds]
            except TypeError:
                win_ann[k] = [v[i] for i in pos_inds]
        win_ann['trunc'] = win_iofs[pos_inds] < 1
        window_anns.append(win_ann)
    return window_anns


def crop_and_save_img(info, windows, window_anns, img_dir, no_padding,
                      padding_value, save_dir, anno_dir, img_ext):
    img = cv2.imread(osp.join(img_dir, info['filename']))
    patch_infos = []
    for i in range(windows.shape[0]):
        patch_info = dict()
        for k, v in info.items():
            if k not in ['id', 'fileanme', 'width', 'height', 'ann']:
                patch_info[k] = v

        window = windows[i]
        x_start, y_start, x_stop, y_stop = window.tolist()
        patch_info['x_start'] = x_start
        patch_info['y_start'] = y_start
        patch_info['id'] = \
            info['id'] + '__' + str(x_stop - x_start) + \
            '__' + str(x_start) + '___' + str(y_start)
        patch_info['ori_id'] = info['id']

        ann = window_anns[i]
        ann['bboxes'] = translate(ann['bboxes'], -x_start,
                                  -y_start)  # 将全图坐标转换为划窗坐标
        patch_info['ann'] = ann

        patch = img[y_start:y_stop, x_start:x_stop]
        if not no_padding:
            height = y_stop - y_start
            width = x_stop - x_start
            if height > patch.shape[0] or width > patch.shape[1]:
                padding_patch = np.empty((height, width, patch.shape[-1]),
                                         dtype=np.uint8)
                if not isinstance(padding_value, (int, float)):
                    assert len(padding_value) == patch.shape[-1]
                padding_patch[...] = padding_value
                padding_patch[:patch.shape[0], :patch.shape[1], ...] = patch
                patch = padding_patch
        patch_info['height'] = patch.shape[0]
        patch_info['width'] = patch.shape[1]

        cv2.imwrite(osp.join(save_dir, patch_info['id'] + img_ext), patch)
        patch_info['filename'] = patch_info['id'] + img_ext
        patch_infos.append(patch_info)

        bboxes_num = patch_info['ann']['bboxes'].shape[0]
        outdir = os.path.join(anno_dir, patch_info['id'] + '.txt')

        with codecs.open(outdir, 'w', 'utf-8') as f_out:
            if bboxes_num == 0:
                pass
            else:
                for idx in range(bboxes_num):
                    obj = patch_info['ann']
                    outline = ' '.join(list(map(str, obj['bboxes'][idx])))
                    diffs = str(obj['diffs']
                                [idx]) if not obj['trunc'][idx] else '2'
                    outline = outline + ' ' + obj['labels'][idx] + ' ' + diffs
                    f_out.write(outline + '\n')

    return patch_infos


def single_split(arguments, sizes, gaps, img_rate_thr, iof_thr, no_padding,
                 padding_value, save_dir, anno_dir, img_ext, lock, prog, total,
                 logger):
    info, img_dir = arguments
    windows = get_sliding_window(info, sizes, gaps, img_rate_thr)
    window_anns = get_window_obj(info, windows, iof_thr)
    patch_infos = crop_and_save_img(info, windows, window_anns, img_dir,
                                    no_padding, padding_value, save_dir,
                                    anno_dir, img_ext)
    assert patch_infos

    lock.acquire()
    prog.value += 1
    msg = f'({prog.value / total:3.1%} {prog.value}:{total})'
    msg += ' - ' + f"Filename: {info['filename']}"
    msg += ' - ' + f"width: {info['width']:<5d}"
    msg += ' - ' + f"height: {info['height']:<5d}"
    msg += ' - ' + f"Objects: {len(info['ann']['bboxes']):<5d}"
    msg += ' - ' + f'Patches: {len(patch_infos)}'
    logger.info(msg)
    lock.release()

    return patch_infos


def setup_logger(log_path):
    logger = logging.getLogger('img split')
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    now = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = osp.join(log_path, now + '.log')
    handlers = [logging.StreamHandler(), logging.FileHandler(log_path, 'w')]

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def translate(bboxes, x, y):
    dim = bboxes.shape[-1]
    translated = bboxes + np.array([x, y] * int(dim / 2), dtype=np.float32)
    return translated


def load_dota(img_dir, ann_dir=None, nproc=10):
    assert osp.isdir(img_dir), f'The {img_dir} is not an existing dir!'
    assert ann_dir is None or osp.isdir(
        ann_dir), f'The {ann_dir} is not an existing dir!'

    print('Starting loading DOTA dataset information.')
    start_time = time.time()
    _load_func = partial(_load_dota_single, img_dir=img_dir, ann_dir=ann_dir)
    if nproc > 1:
        pool = Pool(nproc)
        contents = pool.map(_load_func, os.listdir(img_dir))
        pool.close()
    else:
        contents = list(map(_load_func, os.listdir(img_dir)))
    contents = [c for c in contents if c is not None]
    end_time = time.time()
    print(f'Finishing loading DOTA, get {len(contents)} iamges,',
          f'using {end_time - start_time:.3f}s.')

    return contents


def _load_dota_single(imgfile, img_dir, ann_dir):
    img_id, ext = osp.splitext(imgfile)
    if ext not in ['.jpg', '.JPG', '.png', '.tif', '.bmp']:
        return None

    imgpath = osp.join(img_dir, imgfile)
    size = Image.open(imgpath).size
    txtfile = None if ann_dir is None else osp.join(ann_dir, img_id + '.txt')
    content = _load_dota_txt(txtfile)

    content.update(
        dict(width=size[0], height=size[1], filename=imgfile, id=img_id))
    return content


def _load_dota_txt(txtfile):
    gsd, bboxes, labels, diffs = None, [], [], []
    if txtfile is None:
        pass
    elif not osp.isfile(txtfile):
        print(f"Can't find {txtfile}, treated as empty txtfile")
    else:
        with open(txtfile, 'r') as f:
            for line in f:
                if line.startswith('gsd'):
                    num = line.split(':')[-1]
                    try:
                        gsd = float(num)
                    except ValueError:
                        gsd = None
                    continue

                items = line.split(' ')
                if len(items) >= 9:
                    bboxes.append([float(i) for i in items[:8]])
                    labels.append(items[8])
                    diffs.append(int(items[9]) if len(items) == 10 else 0)

    bboxes = np.array(bboxes, dtype=np.float32) if bboxes else \
        np.zeros((0, 8), dtype=np.float32)
    # labels = np.array(labels, dtype=np.int64) if labels else \
    #         np.zeros((0, ), dtype=np.int64)
    # labels = labels if labels else None
    diffs = np.array(diffs, dtype=np.int64) if diffs else \
        np.zeros((0,), dtype=np.int64)
    ann = dict(bboxes=bboxes, labels=labels, diffs=diffs)
    return dict(gsd=gsd, ann=ann)


def main():
    args = parse_args()

    if args.ann_dirs is None:
        args.ann_dirs = [None for _ in range(len(args.img_dirs))]
    padding_value = args.padding_value[0] \
        if len(args.padding_value) == 1 else args.padding_value
    sizes, gaps = [], []
    for rate in args.rates:
        sizes += [int(size / rate) for size in args.sizes]
        gaps += [int(gap / rate) for gap in args.gaps]
    save_imgs = osp.join(args.save_dir, 'images')
    save_files = osp.join(args.save_dir, 'annfiles')
    os.makedirs(save_imgs)
    os.makedirs(save_files)
    logger = setup_logger(args.save_dir)

    print('Loading original data!!!')
    infos, img_dirs = [], []
    for img_dir, ann_dir in zip(args.img_dirs, args.ann_dirs):
        _infos = load_dota(img_dir=img_dir, ann_dir=ann_dir, nproc=args.nproc)
        _img_dirs = [img_dir for _ in range(len(_infos))]
        infos.extend(_infos)
        img_dirs.extend(_img_dirs)

    print('Start splitting images!!!')
    start = time.time()
    manager = Manager()
    worker = partial(
        single_split,
        sizes=sizes,
        gaps=gaps,
        img_rate_thr=args.img_rate_thr,
        iof_thr=args.iof_thr,
        no_padding=args.no_padding,
        padding_value=padding_value,
        save_dir=save_imgs,
        anno_dir=save_files,
        img_ext=args.save_ext,
        lock=manager.Lock(),
        prog=manager.Value('i', 0),
        total=len(infos),
        logger=logger)

    if args.nproc > 1:
        pool = Pool(args.nproc)
        patch_infos = pool.map(worker, zip(infos, img_dirs))
        pool.close()
    else:
        patch_infos = list(map(worker, zip(infos, img_dirs)))

    patch_infos = reduce(lambda x, y: x + y, patch_infos)
    stop = time.time()
    print(f'Finish splitting images in {int(stop - start)} second!!!')
    print(f'Total images number: {len(patch_infos)}')


if __name__ == '__main__':
    main()
