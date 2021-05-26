import atexit
import os
import warnings
from argparse import ArgumentParser

import mlcrate as mlc
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from fastprogress import master_bar, progress_bar
from iterstrat.ml_stratifiers import MultilabelStratifiedKFold
from omegaconf import OmegaConf
from sklearn.metrics import fbeta_score
from timm.models import *
from timm.models.nfnet import *
from torch.cuda.amp import GradScaler, autocast
from torch.optim import SGD, Adam
from torch.optim.lr_scheduler import (CosineAnnealingWarmRestarts,
                                      ReduceLROnPlateau)
from torch.utils.data import DataLoader

from augmentations.augmentation import met_transform1
from augmentations.strong_aug import *
from dataset import MetDataset
from utils import (find_exp_num, get_logger, remove_abnormal_exp, save_model,
                   seed_everything)

warnings.filterwarnings("ignore")


def parse_args():
    parser = ArgumentParser()
    parser.add_argument("-config", required=True)
    parser.add_argument("options", nargs="*")
    return parser.parse_args()


def main():
    args = parse_args()
    config = OmegaConf.load(args.config)
    config.merge_with_dotlist(args.options)
    atexit.register(
        remove_abnormal_exp, log_path=config.log_path, config_path=config.config_path
    )
    seed_everything(config.seed)

    exp_num = find_exp_num(log_path=config.log_path)
    exp_num = str(exp_num).zfill(3)
    config.weight_path = os.path.join(config.weight_path, f"exp_{exp_num}")
    os.makedirs(config.weight_path, exist_ok=True)
    OmegaConf.save(config, os.path.join(config.config_path, f"exp_{exp_num}.yaml"))
    logger, csv_logger = get_logger(config, exp_num)
    timer = mlc.time.Timer()
    logger.info(mlc.time.now())
    logger.info(f"config: {config}")

    train_df = pd.read_csv(os.path.join(config.root, "train.csv"))
    X = train_df["id"]
    X = np.array([os.path.join(config.root, "train", f"{i}.png") for i in X])
    y = np.load(os.path.join(config.root, "labels.npy"))

    transform = eval(config.transform.name)(config.transform.size)
    logger.info(f"augmentation: {transform}")
    strong_transform = eval(config.strong_transform.name)
    logger.info(f"strong augmentation: {config.strong_transform.name}")

    for fold in range(config.train.n_splits):
        train_idx = np.load(
            os.path.join(config.root, f"train_idx_fold{fold}.npy")
        )
        val_idx = np.load(os.path.join(config.root, f"val_idx_fold{fold}.npy"))

        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]
        train_data = MetDataset("train", X_train, y_train, transform["albu_train"])
        val_data = MetDataset("val", X_val, y_val, transform["albu_val"])
        train_loader = DataLoader(train_data, **config.train_loader)
        val_loader = DataLoader(val_data, **config.val_loader)

        model = eval(config.model)(True)
        if "fc.weight" in model.state_dict().keys():
            model.fc = nn.Linear(model.fc.in_features, config.train.num_labels)
        elif "classifier.weight" in model.state_dict().keys():
            model.classifier = nn.Linear(
                model.classifier.in_features, config.train.num_labels
            )
        elif "head.fc.weight" in model.state_dict().keys():
            model.head.fc = nn.Linear(
                model.head.fc.in_features, config.train.num_labels
            )
        model = model.cuda()
        optimizer = eval(config.optimizer.name)(
            model.parameters(), lr=config.optimizer.lr
        )
        scheduler = eval(config.scheduler.name)(
            optimizer,
            config.train.epoch // config.scheduler.cycle,
            eta_min=config.scheduler.eta_min,
        )
        criterion = eval(config.loss)()
        scaler = GradScaler()

        best_acc = 0
        best_loss = 1e10
        mb = master_bar(range(config.train.epoch))
        for epoch in mb:
            timer.add("train")
            train_loss, train_acc = train(
                config,
                model,
                transform["torch_train"],
                strong_transform,
                train_loader,
                optimizer,
                criterion,
                mb,
                epoch,
                scaler,
            )
            train_time = timer.fsince("train")

            timer.add("val")
            val_loss, val_acc = validate(
                config, model, transform["torch_val"], val_loader, criterion, mb, epoch
            )
            val_time = timer.fsince("val")

            output1 = "epoch: {} train_time: {} validate_time: {}".format(
                epoch, train_time, val_time
            )
            output2 = "train_loss: {:.3f} train_acc: {:.3f} val_loss: {:.3f} val_acc: {:.3f}".format(
                train_loss, train_acc, val_loss, val_acc
            )
            logger.info(output1)
            logger.info(output2)
            mb.write(output1)
            mb.write(output2)
            csv_logger.write([epoch, train_loss, train_acc, val_loss, val_acc])

            scheduler.step()

            if val_loss < best_loss:
                best_loss = val_loss
                save_name = os.path.join(
                    config.weight_path, f"best_loss_fold{fold}.pth"
                )
                save_model(save_name, epoch, val_loss, val_acc, model, optimizer)
            if val_acc > best_acc:
                best_acc = val_acc
                save_name = os.path.join(config.weight_path, f"best_acc_fold{fold}.pth")
                save_model(save_name, epoch, val_loss, val_acc, model, optimizer)

            save_name = os.path.join(config.weight_path, f"last_epoch_fold{fold}.pth")
            save_model(save_name, epoch, val_loss, val_acc, model, optimizer)


@torch.enable_grad()
def train(
    config,
    model,
    transform,
    strong_transform,
    loader,
    optimizer,
    criterion,
    mb,
    epoch,
    scaler,
):
    preds = []
    gt = []
    losses = []
    scores = []

    model.train()
    for it, (images, labels) in enumerate(progress_bar(loader, parent=mb)):
        images = images.cuda()
        labels = labels.cuda()
        images = transform(images)
        if epoch < config.train.epoch - 5:
            with autocast():
                images, labels_a, labels_b, lam = strong_transform(
                    images, labels, **config.strong_transform.params
                )
                logits = model(images)
                loss = criterion(logits, labels_a) * lam + criterion(
                    logits, labels_b
                ) * (1 - lam)
                loss /= config.train.accumulate
        else:
            with autocast():
                logits = model(images)
                loss = criterion(logits, labels)
                loss /= config.train.accumulate

        scaler.scale(loss).backward()
        if (it + 1) % config.train.accumulate == 0:
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad()

        logits = (logits.sigmoid() > 0.2).detach().cpu().numpy().astype(int)
        labels = labels.detach().cpu().numpy().astype(int)
        score = fbeta_score(labels, logits, beta=2, average="samples")
        scores.append(score)
        preds.append(logits)
        gt.append(labels)
        losses.append(loss.item())

        mb.child.comment = (
            "loss: {:.3f} avg_loss: {:.3f} acc: {:.3f} avg_acc: {:.3f}".format(
                loss.item(),
                np.mean(losses),
                score,
                np.mean(scores),
            )
        )

    preds = np.concatenate(preds)
    gt = np.concatenate(gt)
    score = fbeta_score(gt, preds, beta=2, average="samples")
    return np.mean(losses), score


@torch.no_grad()
def validate(config, model, transform, loader, criterion, mb, device):
    preds = []
    gt = []
    losses = []

    model.eval()
    for it, (images, labels) in enumerate(progress_bar(loader, parent=mb)):
        images = images.cuda()
        labels = labels.cuda()
        images = transform(images)

        logits = model(images)
        loss = criterion(logits, labels) / config.train.accumulate

        logits = logits.sigmoid().cpu().numpy()
        labels = labels.cpu().numpy().astype(int)
        preds.append(logits)
        gt.append(labels)
        losses.append(loss.item())

    preds = np.concatenate(preds)
    gt = np.concatenate(gt)
    best_score = 0
    for th in np.arange(0.05, 0.5, 0.05):
        score = fbeta_score(gt, (preds > th).astype(int), beta=2, average="samples")
        best_score = max(best_score, score)
    return np.mean(losses), best_score


if __name__ == "__main__":
    main()
