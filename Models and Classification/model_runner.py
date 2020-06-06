import math
import os
from helpers.logger import Logger

import torch
import torch.nn as nn
from transformers import BertModel
from sklearn import metrics
from torch.utils.data import DataLoader, random_split

from data_utils import (ABSADataset, Tokenizer4Bert, build_embedding_matrix,
                        build_tokenizer)

torch.set_printoptions(profile="full")

class ModelRunner:
    def __init__(self, opt):

        self.opt = opt
        self.logger = Logger(opt.model_name, opt.dataset)

        if 'bert' in opt.model_name:
            tokenizer = Tokenizer4Bert(
                opt.max_seq_len,
                opt.pretrained_bert_name)
            bert = BertModel.from_pretrained(opt.pretrained_bert_name)
            self.model = opt.model_class(bert, opt).to(opt.device)
        else:
            tokenizer = build_tokenizer(
                fnames=[opt.dataset_file['train'], opt.dataset_file['test']],
                max_seq_len=opt.max_seq_len,
                dat_fname='{0}_tokenizer.dat'.format(opt.dataset))
            embedding_matrix = build_embedding_matrix(
                word2idx=tokenizer.word2idx,
                embed_dim=opt.embed_dim,
                dat_fname='{0}_{1}_embedding_matrix.dat'.format(str(opt.embed_dim), opt.dataset))
            self.model = opt.model_class(embedding_matrix, opt).to(opt.device)

        self.trainset = ABSADataset(opt.dataset_file['train'], tokenizer)
        self.testset = ABSADataset(opt.dataset_file['test'], tokenizer)
        assert 0 <= opt.valset_ratio < 1
        if opt.valset_ratio > 0:
            valset_len = int(len(self.trainset) * opt.valset_ratio)
            self.trainset, self.valset = random_split(self.trainset, (len(self.trainset)-valset_len, valset_len))
        else:
            self.valset = self.testset

        if opt.device.type == 'cuda':
            self.logger.log('cuda memory allocated: {}'.format(torch.cuda.memory_allocated(device=opt.device.index)))
        self._print_args()

    def _print_args(self):
        n_trainable_params, n_nontrainable_params = 0, 0
        for p in self.model.parameters():
            n_params = torch.prod(torch.tensor(p.shape))
            if p.requires_grad:
                n_trainable_params += n_params
            else:
                n_nontrainable_params += n_params
        self.logger.log('n_trainable_params: {0}, n_nontrainable_params: {1}'.format(n_trainable_params, n_nontrainable_params))
        self.logger.log('> training arguments:')
        for arg in vars(self.opt):
            self.logger.log('>>> {0}: {1}'.format(arg, getattr(self.opt, arg)))

    def _reset_params(self):
        for child in self.model.children():
            if type(child) != BertModel:  # skip bert params
                for p in child.parameters():
                    if p.requires_grad:
                        if len(p.shape) > 1:
                            self.opt.initializer(p)
                        else:
                            stdv = 1. / math.sqrt(p.shape[0])
                            torch.nn.init.uniform_(p, a=-stdv, b=stdv)

    def _train(self, criterion, optimizer, train_data_loader, val_data_loader):
        max_val_acc = 0
        max_val_f1 = 0
        global_step = 0
        path = None

        tmp_best_model = None
        best_path = None

        for epoch in range(self.opt.num_epoch):
            # self.logger.log('>' * 100)
            self.logger.log('epoch: {}'.format(epoch))
            n_correct, n_total, loss_total = 0, 0, 0
            # switch model to training mode
            self.model.train()
            for i_batch, sample_batched in enumerate(train_data_loader):
                global_step += 1
                # clear gradient accumulators
                optimizer.zero_grad()

                inputs = [sample_batched[col].to(self.opt.device) for col in self.opt.model_inputs]
                outputs = self.model(inputs)
                targets = sample_batched['polarity'].to(self.opt.device)

                loss = criterion(outputs, targets)
                loss.backward()
                optimizer.step()

                n_correct += (torch.argmax(outputs, -1) == targets).sum().item()
                n_total += len(outputs)
                loss_total += loss.item() * len(outputs)
                if global_step % self.opt.log_step == 0:
                    train_acc = n_correct / n_total
                    train_loss = loss_total / n_total
                    logstr = 'loss: {:.4f}, acc: {:.4f}'.format(
                      train_loss, train_acc)
                    print(logstr)
                    # self.logger.log(logstr)

            val_acc, val_f1 = self._evaluate_acc_f1(val_data_loader)
            self.logger.log('> val_acc: {:.4f}, val_f1: {:.4f}'.format(val_acc, val_f1))
            # if val_acc > max_val_acc:
                # max_val_acc = val_acc
            if val_f1 > max_val_f1:
                max_val_f1 = val_f1
                self.logger.log("New best model found, F1 = {}".format(
                  max_val_f1))
                if not os.path.exists('state_dict'):
                    os.mkdir('state_dict')

                best_path = 'state_dict/{0}_{1}_val_acc{2}_val_f{3}.tmp'.format(self.opt.model_name, self.opt.dataset, round(val_acc, 4), round(val_f1, 4))
                tmp_best_model = self.model.state_dict()

        # if tmp_best_model:
        #     torch.save(tmp_best_model, best_path)
        #     self.logger.log('>> saved: {}'.format(best_path))

        #return best_path
        return tmp_best_model

    def _evaluate_acc_f1(self, data_loader):
        n_correct, n_total = 0, 0
        t_targets_all, t_outputs_all = None, None
        # switch model to evaluation mode
        self.model.eval()
        with torch.no_grad():
            for t_batch, t_sample_batched in enumerate(data_loader):
                t_inputs = [t_sample_batched[col].to(self.opt.device) for col in self.opt.model_inputs]
                t_targets = t_sample_batched['polarity'].to(self.opt.device)
                t_outputs = self.model(t_inputs)

                n_correct += (torch.argmax(t_outputs, -1) == t_targets).sum().item()
                n_total += len(t_outputs)

                if t_targets_all is None:
                    t_targets_all = t_targets
                    t_outputs_all = t_outputs
                else:
                    t_targets_all = torch.cat((t_targets_all, t_targets), dim=0)
                    t_outputs_all = torch.cat((t_outputs_all, t_outputs), dim=0)

        acc = n_correct / n_total
        cpu_tar = t_targets_all.cpu()
        cpu_out = torch.argmax(t_outputs_all.cpu(), -1).cpu()
        self.logger.log("True: {}".format(cpu_tar))
        self.logger.log("Predicted: {}".format(cpu_out))
        f1 = metrics.f1_score(cpu_tar, cpu_out, labels=[0, 1, 2], average='macro')
        return acc, f1

    def run(self):
        # Loss and Optimizer
        criterion = nn.CrossEntropyLoss()
        _params = filter(lambda p: p.requires_grad, self.model.parameters())
        optimizer = self.opt.optimizer(_params, lr=self.opt.learning_rate, weight_decay=self.opt.l2reg)

        train_data_loader = DataLoader(dataset=self.trainset, batch_size=self.opt.batch_size, shuffle=True)
        test_data_loader = DataLoader(dataset=self.testset, batch_size=self.opt.batch_size, shuffle=False)
        val_data_loader = DataLoader(dataset=self.valset, batch_size=self.opt.batch_size, shuffle=False)

        self._reset_params()
        # best_model_path = self._train(criterion, optimizer, train_data_loader, val_data_loader)
        # self.model.load_state_dict(torch.load(best_model_path))

        # return best state dict
        best_model = self._train(
          criterion,
          optimizer,
          train_data_loader,
          val_data_loader
        )
        self.model.load_state_dict(best_model)
        self.model.eval()
        test_acc, test_f1 = self._evaluate_acc_f1(test_data_loader)
        self.logger.log(
          '>> test_acc: {:.4f}, test_f1: {:.4f}'.format(test_acc, test_f1)
        )
        save_name = 'state_dict/{}_ACC{}_F{}.{}'.format(
          self.opt.dataset,
          round(test_acc, 4),
          round(test_f1, 4),
          self.opt.model_name
        )
        # torch.save(self.model.state_dict(), save_name)
