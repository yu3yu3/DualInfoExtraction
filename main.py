import gc
import os
import sys
import time
import pickle
import random
import logging

import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np

from model.Dualnet import DualNet

from utils.data import Data
from utils.functions import *
from options import parse_argument
from utils.metric import get_ner_fmeasure

import warnings
warnings.filterwarnings('ignore')

seed_num = 42
random.seed(seed_num)
torch.manual_seed(seed_num)
np.random.seed(seed_num)

logger = logging.getLogger(__name__)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO,
                    filename='output/output.log')
logger.info("\n\n")
logger.info("Start Status Logging...")

def lr_decay(optimizer, epoch, decay_rate, init_lr):
    lr = init_lr/(1+decay_rate*epoch)
    print("  Learning rate is set as:", lr)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return optimizer

def evaluate(data, model, name):
    if name == "train":
        instances = data.train_Ids
    elif name == "dev":
        instances = data.dev_Ids
    elif name == 'test':
        instances = data.test_Ids
    else:
        print("Error: wrong evaluate name,", name)
        exit(1)
    right_token = 0
    whole_token = 0
    nbest_pred_results = []
    pred_scores = []
    pred_results = []
    gold_results = []
    ## set model in eval model
    model.eval()
    batch_size = model.batch_size
    start_time = time.time()
    train_num = len(instances)
    total_batch = train_num//batch_size+1
    for batch_id in range(total_batch):
        start = batch_id*batch_size
        end = (batch_id+1)*batch_size
        if end > train_num:
            end =  train_num
        instance = instances[start:end]
        if not instance:
            continue
        batch_word, batch_wordlen, batch_wordrecover, batch_char, batch_charlen, batch_charrecover, batch_label, mask  = batchify_sequence_labeling_with_label(instance, args.gpu,args.max_sent_length, False)
        tag_seq = model(batch_word, batch_wordlen, batch_char, batch_charlen, batch_charrecover, mask)
        # print("tag:",tag_seq)
        pred_label, gold_label = recover_label(tag_seq, batch_label, mask, data.label_alphabet, batch_wordrecover)
        pred_results += pred_label
        gold_results += gold_label
    decode_time = time.time() - start_time
    speed = len(instances)/decode_time

    acc, p, r, f = get_ner_fmeasure(gold_results, pred_results, data.tagScheme)
    return speed, acc, p, r, f, pred_results

def train(args,data,model):
    logger.info("Training model...")
    model.show_model_summary(logger)
    print("Training Parameters:%s",args)

    if args.optimizer.lower() == "sgd":
        optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum,weight_decay=args.l2)
    elif args.optimizer.lower() == "adagrad":
        optimizer = optim.Adagrad(model.parameters(), lr=args.lr, weight_decay=args.l2)
    elif args.optimizer.lower() == "adadelta":
        optimizer = optim.Adadelta(model.parameters(), lr=args.lr, weight_decay=args.l2)
    elif args.optimizer.lower() == "rmsprop":
        optimizer = optim.RMSprop(model.parameters(), lr=args.lr, weight_decay=args.l2)
    elif args.optimizer.lower() == "adam":
        optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.l2)
    else:
        print("Optimizer illegal: %s"%(args.optimizer))
        exit(1)
    best_dev = -10

    ## start training
    for idx in range(args.iteration):
        epoch_start = time.time()
        temp_start = epoch_start
        #print("Epoch: %s/%s" %(idx,model.iteration))
        if args.optimizer == "SGD":
            optimizer = lr_decay(optimizer, idx, args.lr_decay, args.lr)
        instance_count = 0
        sample_id = 0
        sample_loss = 0
        total_loss = 0
        right_token = 0
        whole_token = 0
        random.shuffle(data.train_Ids)
        #print("Shuffle: first input word list:", data.train_Ids[0][0])
        ## set model in train model
        model.train()
        model.zero_grad()
        batch_size = args.batch_size
        batch_id = 0
        train_num = len(data.train_Ids)
        total_batch = train_num//batch_size+1

        for batch_id in range(total_batch):
            start = batch_id*batch_size
            end = (batch_id+1)*batch_size
            if end >train_num:
                end = train_num
            instance = data.train_Ids[start:end]
            if not instance:
                continue
            batch_word, batch_wordlen, batch_wordrecover, batch_char, batch_charlen, batch_charrecover, batch_hlabel,batch_llabel, mask  =\
                batchify_sequence_labeling_with_label(instance, args.gpu,args.max_sent_length,True)
            instance_count += 1
            H2BH_loss,H2BB_loss,B2HB_loss,B2HH_loss,H2BH_tag_seqs,H2BB_tag_seqs,B2HB_tag_seqs,B2HH_tag_seqs = model.calculate_loss(batch_word, batch_wordlen, batch_char, batch_charlen, batch_charrecover, batch_hlabel,batch_llabel, mask)

            #todo change to evaluate both layer tag....
            right, whole = predict_check(H2BB_tag_seqs, batch_llabel, mask)
            right_token += right
            whole_token += whole
            # print("loss:",loss.item())

            loss = H2BH_loss + H2BB_loss + B2HB_loss + B2HH_loss
            sample_loss += loss.item()
            total_loss += loss.item()
            if end%10 == 0:
                temp_time = time.time()
                temp_cost = temp_time - temp_start
                temp_start = temp_time
                print("     Instance: %s; Time: %.2fs; loss: %.4f; acc: %s/%s=%.4f"%(end, temp_cost, sample_loss, right_token, whole_token,(right_token+0.)/whole_token))
                if sample_loss > 1e8 or str(sample_loss) == "nan":
                    print("ERROR: LOSS EXPLOSION (>1e8) ! PLEASE SET PROPER PARAMETERS AND STRUCTURE! EXIT....")
                    exit(1)
                sys.stdout.flush()
                sample_loss = 0
            loss.backward()
            if args.clip:
                torch.nn.utils.clip_grad_norm_(model.parameters(),args.clip)
            optimizer.step()
            model.zero_grad()
        temp_time = time.time()
        temp_cost = temp_time - temp_start
        print("     Instance: %s; Time: %.2fs; loss: %.4f; acc: %s/%s=%.4f"%(end, temp_cost, sample_loss, right_token, whole_token,(right_token+0.)/whole_token))

        epoch_finish = time.time()
        epoch_cost = epoch_finish - epoch_start
        logger.info("Epoch: %s training finished. Time: %.2fs, speed: %.2fst/s,  total loss: %s"%(idx, epoch_cost, train_num/epoch_cost, total_loss))
        print("Epoch: %s training finished. Time: %.2fs, speed: %.2fst/s,  total loss: %s" % (idx, epoch_cost, train_num / epoch_cost, total_loss))
        #print("totalloss:", total_loss)
        if total_loss > 1e8 or str(total_loss) == "nan":
            print("ERROR: LOSS EXPLOSION (>1e8) ! PLEASE SET PROPER PARAMETERS AND STRUCTURE! EXIT....")
            exit(1)

        # continue
        speed, acc, p, r, f, _ = evaluate(data, model, "dev")
        dev_finish = time.time()
        dev_cost = dev_finish - epoch_finish

        current_score = f
        logger.info("Dev: time: %.2fs, speed: %.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f,best_f: %.4f"%(dev_cost, speed, acc, p, r, f,best_dev))
        print("Dev: time: %.2fs, speed: %.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f,best_f: %.4f" % (dev_cost, speed, acc, p, r, f,best_dev))
        if current_score > best_dev:
            print("New f score %f > previous %f ,Save current best model in file:%s" % (current_score,best_dev,args.load_model_name))
            torch.save(model.state_dict(), args.load_model_name)
            best_dev = current_score
        gc.collect()

def load_model_decode(args,data,model,name):
    print("Load Model from file: ", args.model_dir)

    model.load_state_dict(torch.load(args.load_model_name))

    print("Decode %s data ..."% name)
    start_time = time.time()
    speed, acc, p, r, f, pred_results= evaluate(data, model, name)
    end_time = time.time()
    time_cost = end_time - start_time
    print("%s: time:%.2fs, speed:%.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f"%(name, time_cost, speed, acc, p, r, f))
    return pred_results

if __name__ == '__main__':

    args = parse_argument()
    args.gpu = torch.cuda.is_available()

    #Load data
    data = Data(args)
    if args.load_data :
        print("Load data from Pkl file...")
        data.load(args.load_data_name)
    else:
        print("Generating Pickle data file...")
        data.build_alphabet(data.train_dir)
        data.build_alphabet(data.dev_dir)
        data.build_alphabet(data.test_dir)
        data.fix_alphabet()

        data.generate_instance('train')
        data.generate_instance('dev')
        data.generate_instance('test')
        data.build_pretrain_emb()
        print("Saving pkl data %s..." % args.load_data_name)
        if not os.path.exists(args.output_dir):
            os.mkdir(args.output_dir)
        data.save(args.load_data_name)

    #initial model...
    model = DualNet(args,data)

    if args.gpu:
        model.to(torch.device("cuda"))

    print("Seed num:" ,seed_num)
    status = args.status.lower()

    if status == 'train':
        print("MODEL: train")
        train(args,data,model)

    elif status == 'test':
        print("MODEL: test")
        print("Load Model from file: ", args.load_model_name)
        model.load_state_dict(torch.load(args.load_model_name))
        model.show_model_summary(logger)
        test_start = time.time()
        speed, acc, p, r, f, _= evaluate(data, model, "test")
        test_cost = time.time() - test_start
        print("Test: time: %.2fs, speed: %.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f"%(test_cost, speed, acc, p, r, f))
        logger.info("Test: time: %.2fs, speed: %.2fst/s; acc: %.4f, p: %.4f, r: %.4f, f: %.4f" % (test_cost, speed, acc, p, r, f))

    elif status == 'decode':
        print("MODEL: decode")
        decode_results, pred_scores = load_model_decode(args,data,model,'test')
        data.write_decoded_results(decode_results, 'test')
    else:
        print("Invalid argument! Please use valid arguments! (train/test/decode)")