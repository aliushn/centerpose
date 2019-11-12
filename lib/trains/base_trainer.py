from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import torch
import torch.nn as nn
from progress.bar import Bar
from models.data_parallel import DataParallel
from utils.utils import AverageMeter


class ModleWithLoss(torch.nn.Module):
    def __init__(self, model, loss):
        super(ModleWithLoss, self).__init__()
        self.model = model
        self.loss = loss

    def forward(self, batch):
        outputs = self.model(batch['input'])
        loss, loss_stats = self.loss(outputs, batch)
        return outputs[-1], loss, loss_stats


class BaseTrainer(object):
    def __init__(
        self, cfg, local_rank, model, optimizer=None):
        self.cfg = cfg
        self.optimizer = optimizer
        self.loss_stats, self.loss = self._get_losses(cfg, local_rank)
        self.model_with_loss = ModleWithLoss(model, self.loss)
        self.local_rank = local_rank

    def set_device(self, gpus, chunk_sizes, device):
    
        if  self.cfg.TRAIN.DISTRIBUTE:
            self.model_with_loss = self.model_with_loss.to(device)
            self.model_with_loss = nn.parallel.DistributedDataParallel(self.model_with_loss,
                                                        device_ids=[self.local_rank, ],
                                                        output_device=self.local_rank)
        else:
            self.model_with_loss = nn.DataParallel(self.model_with_loss).to(device)
    
        for state in self.optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device=device, non_blocking=True)

    def run_epoch(self, phase, epoch, data_loader):
        model_with_loss = self.model_with_loss
        if phase == 'train':
            model_with_loss.train()
            
        else:
            if len(self.cfg.GPUS) > 1:
                model_with_loss = self.model_with_loss.module        
            model_with_loss.eval()
            torch.cuda.empty_cache()

        cfg = self.cfg
        results = {}
        data_time, batch_time = AverageMeter(), AverageMeter()
        avg_loss_stats = {l: AverageMeter() for l in self.loss_stats}
        num_iters = len(data_loader)
        bar = Bar('{}/{}'.format(cfg.TASK, cfg.EXP_ID), max=num_iters)
        end = time.time()
        for iter_id, batch in enumerate(data_loader):
            if iter_id >= num_iters:
                break
            data_time.update(time.time() - end)

            for k in batch:
                if k != 'meta':
                    batch[k] = batch[k].to(device=torch.device('cuda:%d'%self.local_rank), non_blocking=True)    
            output, loss, loss_stats = model_with_loss(batch)
            loss = loss.mean()
            if phase == 'train':
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
            batch_time.update(time.time() - end)
            end = time.time()

            Bar.suffix = '{phase}: [{0}][{1}/{2}]|Tot: {total:} |ETA: {eta:} '.format(
                epoch, iter_id, num_iters, phase=phase,
                total=bar.elapsed_td, eta=bar.eta_td)
            for l in avg_loss_stats:
                avg_loss_stats[l].update(
                  loss_stats[l].mean().item(), batch['input'].size(0))
                Bar.suffix = Bar.suffix + '|{} {:.4f} '.format(l, avg_loss_stats[l].avg)
            if not cfg.TRAIN.HIDE_DATA_TIME:
                Bar.suffix = Bar.suffix + '|Data {dt.val:.3f}s({dt.avg:.3f}s) ' \
                    '|Net {bt.avg:.3f}s'.format(dt=data_time, bt=batch_time)
            if cfg.PRINT_FREQ > 0:
                if iter_id % cfg.PRINT_FREQ == 0:
                    print('{}/{}| {}'.format(cfg.TASK, cfg.EXP_ID, Bar.suffix)) 
            else:
                bar.next()
      
            if cfg.DEBUG > 0:
                self.debug(batch, output, iter_id)
      
            if phase == 'val':
                self.save_result(output, batch, results)
            del output, loss, loss_stats
    
        bar.finish()
        ret = {k: v.avg for k, v in avg_loss_stats.items()}
        ret['time'] = bar.elapsed_td.total_seconds() / 60.
        
        return ret, results

    def debug(self, batch, output, iter_id):
        raise NotImplementedError

    def save_result(self, output, batch, results):
        raise NotImplementedError

    def _get_losses(self, cfg):
        raise NotImplementedError

    def val(self, epoch, data_loader):
        return self.run_epoch('val', epoch, data_loader)

    def train(self, epoch, data_loader):
        return self.run_epoch('train', epoch, data_loader)
