#  Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "1,2"
import sys
import time
import logging
import argparse
import ast
import numpy as np
import cv2
import random

import paddle
import paddle.fluid as fluid

from PIL import Image
from utils.config_utils import *
import models
from reader import get_reader
from metrics import get_metrics
from utils.utility import check_cuda
from utils.utility import check_version
from reader.kinetics_reader import imgs_transform

logging.root.handlers = []
FORMAT = '[%(levelname)s: %(filename)s: %(lineno)4d]: %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT, stream=sys.stdout)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--extractor_config',
        type=str,
        default='configs/tsn.yaml',
        help='path to config file of model')
    parser.add_argument(
        '--extractor_name',
        type=str,
        default='TSN',
        help='extractor model name, default TSN')
    parser.add_argument(
        '--predictor_config',
        '--pconfig',
        type=str,
        default='configs/attention_lstm.yaml',
        help='path to config file of model')
    parser.add_argument(
        '--predictor_name',
        '--pname',
        type=str,
        default='AttentionLSTM',
        help='predictor model name, as AttentionLSTM, AttentionCluster, NEXTVLAD'
    )
    parser.add_argument(
        '--use_gpu',
        type=ast.literal_eval,
        default=True,
        help='default use gpu.')
    parser.add_argument(
        '--extractor_weights',
        type=str,
        default='weights/tsn',
        help='extractor weight path')
    parser.add_argument(
        '--predictor_weights',
        '--pweights',
        type=str,
        default='weights/attention_lstm',
        help='predictor weight path')
    parser.add_argument(
        '--filelist',
        type=str,
        default='./data/VideoTag_test.list',
        help='path of video data, multiple video')
    parser.add_argument(
        '--video_path',
        type=str,
        default="./data/mp4/5.mp4"
    )
    parser.add_argument(
        '--save_dir',
        type=str,
        default='data/VideoTag_results',
        help='output file path')
    parser.add_argument(
        '--label_file',
        type=str,
        default='label_3396.txt',
        help='chinese label file path')

    args = parser.parse_args()
    return args


def decode_mp4(video_path, mode="infer", seg_num=300, seglen=1, short_size=256, target_size=224,
               img_mean=[[[0.485]], [[0.456]], [[0.406]]], img_std=[[[0.229]], [[0.224]], [[0.225]]]):
    mp4_path = video_path
    label = video_path
    try:
        imgs = mp4_loader(mp4_path, seg_num, seglen, mode)
        if len(imgs) < 1:
            logger.error('{} frame length {} less than 1.'.format(
                mp4_path, len(imgs)))
            return None, None
    except:
        logger.error('Error when loading {}'.format(mp4_path))
        return None, None

    return imgs_transform(imgs, mode, seg_num, seglen, \
                          short_size, target_size, img_mean, img_std), label


def mp4_loader(filepath, nsample, seglen, mode):
    cap = cv2.VideoCapture(filepath)
    videolen = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    sampledFrames = []
    for i in range(videolen):
        ret, frame = cap.read()
        # maybe first frame is empty
        if ret == False:
            continue
        img = frame[:, :, ::-1]
        sampledFrames.append(img)
    average_dur = int(len(sampledFrames) / nsample)
    imgs = []
    for i in range(nsample):
        idx = 0
        if mode == 'train':
            if average_dur >= seglen:
                idx = random.randint(0, average_dur - seglen)
                idx += i * average_dur
            elif average_dur >= 1:
                idx += i * average_dur
            else:
                idx = i
        else:
            if average_dur >= seglen:
                idx = (average_dur - 1) // 2
                idx += i * average_dur
            elif average_dur >= 1:
                idx += i * average_dur
            else:
                idx = i

        for jj in range(idx, idx + seglen):
            imgbuf = sampledFrames[int(jj % len(sampledFrames))]
            img = Image.fromarray(imgbuf, mode='RGB')
            imgs.append(img)

    return imgs

def main():
    """
    Video classification model of 3000 Chinese tags.
    videotag_extractor_prdictor (as videotag_TSN_AttentionLSTM)
    two stages in our model:
        1. extract feature from input video(mp4 format) using extractor
        2. predict classification results from extracted feature  using predictor
    we implement this using two name scopes, ie. extractor_scope and predictor_scope.
    """

    if not os.path.isdir(args.save_dir):
        os.makedirs(args.save_dir)
    extractor_config = parse_config(args.extractor_config)
    extractor_infer_config = merge_configs(extractor_config, 'infer',
                                           vars(args))
    print(extractor_infer_config)
    extractor_start_time = time.time()
    extractor_scope = fluid.Scope()
    # paddle 2.0rc 默认是动态图  因此需要开启静态图模式
    paddle.enable_static()
    with fluid.scope_guard(extractor_scope):
        extractor_startup_prog = fluid.Program()
        extractor_main_prog = fluid.Program()
        with fluid.program_guard(extractor_main_prog, extractor_startup_prog):
            with fluid.unique_name.guard():
                # build model
                extractor_model = models.get_model(
                    args.extractor_name,
                    extractor_infer_config,
                    mode='infer',
                    is_videotag=True)
                # input_shape = [None, 300, 3 ,target_size, target_size]
                extractor_model.build_input(use_dataloader=False)
                # model_output : (None, 300, 2048)
                extractor_model.build_model()
                # get model input : self.feature_input = [image]
                # input_shape = [None, 300, 3 ,target_size, target_size]
                extractor_feeds = extractor_model.feeds()
                # get model output : self.network_outputs
                # model_output : (None, 300, 2048)
                extractor_fetch_list = extractor_model.fetches()

                place = fluid.CUDAPlace(0) if args.use_gpu else fluid.CPUPlace()
                exe = fluid.Executor(place)

                exe.run(extractor_startup_prog)

                logger.info('load extractor weights from {}'.format(
                    args.extractor_weights))

                extractor_model.load_pretrain_params(
                    exe, args.extractor_weights, extractor_main_prog)

                # get reader and metrics
                # print(args.extractor_name)  # TNS
                extractor_reader = get_reader(args.extractor_name, 'infer',
                                              extractor_infer_config)
                extractor_feeder = fluid.DataFeeder(
                    place=place, feed_list=extractor_feeds)

                feature_list = []
                file_list = []
                data = decode_mp4(args.video_path)

                file_id = data[1]
                feed_data = [[data[0]]]
                feature_out = exe.run(fetch_list=extractor_fetch_list,
                                      feed=extractor_feeder.feed(feed_data))

                print(feature_out[0].shape)  # TSN 提取的特征（1，300，2048）  300：300帧

                feature_list.append(feature_out[0])  # get out from list
                file_list.append(file_id)
                logger.info(
                    '========[Stage 1 Sample {} ] Extractor finished======'.
                        format(file_id))
        extractor_end_time = time.time()
        print('extractor_time', extractor_end_time - extractor_start_time)

    predictor_config = parse_config(args.predictor_config)
    predictor_infer_config = merge_configs(predictor_config, 'infer',
                                           vars(args))

    # get Predictor input from Extractor output
    predictor_feed_list = []
    for i in range(len(feature_list)):
        feature_out = feature_list[i]
        if args.predictor_name == "AttentionCluster":
            extractor_seg_num = extractor_infer_config.INFER.seg_num
            predictor_seg_num = predictor_infer_config.MODEL.seg_num
            idxs = []
            stride = float(extractor_seg_num) / predictor_seg_num
            for j in range(predictor_seg_num):
                pos = (j + np.random.random()) * stride
                idxs.append(min(extractor_seg_num - 1, int(pos)))
            extractor_feature = feature_out[:, idxs, :].astype(
                float)  # get from bs dim
        else:
            extractor_feature = feature_out.astype(float)
        predictor_feed_data = [extractor_feature]
        predictor_feed_list.append((predictor_feed_data, file_list[i]))

    predictor_start_time = time.time()
    predictor_scope = fluid.Scope()
    with fluid.scope_guard(predictor_scope):
        predictor_startup_prog = fluid.Program()
        predictor_main_prog = fluid.Program()
        with fluid.program_guard(predictor_main_prog, predictor_startup_prog):
            with fluid.unique_name.guard():
                # parse config
                predictor_model = models.get_model(
                    args.predictor_name, predictor_infer_config, mode='infer')
                predictor_model.build_input(use_dataloader=False)
                predictor_model.build_model()
                predictor_feeds = predictor_model.feeds()

                exe.run(predictor_startup_prog)

                logger.info('load predictor weights from {}'.format(
                    args.predictor_weights))
                predictor_model.load_test_weights(exe, args.predictor_weights,
                                                  predictor_main_prog)

                predictor_feeder = fluid.DataFeeder(
                    place=place, feed_list=predictor_feeds)
                predictor_fetch_list = predictor_model.fetches()
                predictor_metrics = get_metrics(args.predictor_name.upper(),
                                                'infer', predictor_infer_config)
                predictor_metrics.reset()

                for idx, data in enumerate(predictor_feed_list):
                    file_id = data[1]
                    predictor_feed_data = data[0]
                    final_outs = exe.run(
                        fetch_list=predictor_fetch_list,
                        feed=predictor_feeder.feed(predictor_feed_data))
                    logger.info(
                        '=======[Stage 2 Sample {} ] Predictor finished========'.
                            format(file_id))

                    #### attention lstm outputs #######

                    print(final_outs[0].shape)  # (1, 3396)  模型预测输出 3396个类
                    print(final_outs[1].shape)  # (1, 4096)  lstm attention整合后的4096维的embeding
                    print(final_outs[2].shape)  # (1, 8192)  lstm attention整合后的8192维的embeding

    #                 final_outs = final_outs[0:1]
    #                 print(file_id)
    #                 final_result_list = [item
    #                                      for item in final_outs] + [[file_id]]
    #
    #                 predictor_metrics.accumulate(final_result_list)
    #             predictor_metrics.finalize_and_log_out(
    #                 savedir=args.save_dir, label_file=args.label_file)
    predictor_end_time = time.time()
    print('predictor_time', predictor_end_time - predictor_start_time)


if __name__ == '__main__':
    start_time = time.time()
    args = parse_args()
    print(args)
    check_cuda(args.use_gpu)
    check_version()
    logger.info(args)
    main()
    end_time = time.time()
    period = end_time - start_time
    print('[INFER] infer finished. cost time: {}'.format(period))

