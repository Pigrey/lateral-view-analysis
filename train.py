import argparse
from glob import glob
import os
from os.path import join, exists
import pickle

import numpy as np

import torch
from torch.optim import Adam
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torch import nn
from torchvision.transforms import Compose

from dataset import PCXRayDataset, Normalize, ToTensor, RandomRotation, GaussianNoise, ToPILImage, split_dataset
from densenet import DenseNet, add_dropout
from hemis import Hemis, add_dropout_hemis, JointConcatModel, MultiTaskModel

from sklearn.metrics import roc_auc_score, average_precision_score, accuracy_score
import pandas as pd


def train(data_dir, csv_path, splits_path, output_dir, target='pa', nb_epoch=100, learning_rate=1e-4, batch_size=1,
          dropout=None, pretrained=False, min_patients_per_label=50, seed=666, data_augmentation=True,
          joint_model_type='hemis', merge_at=2, combine_at='prepool', join_how='concat', loss_wts=None,
          vote_at_test=False):
    assert target in ['pa', 'l', 'joint']

    torch.manual_seed(seed)
    np.random.seed(seed)

    output_dir = output_dir.format(seed)
    output_dir = join('./logs', output_dir)
    splits_path = splits_path.format(seed)

    print("Training mode: {}".format(target))
    
    if not exists(output_dir):
        os.makedirs(output_dir)

    if not exists(splits_path):
        split_dataset(csv_path, splits_path, seed=seed)

    # Find device
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print('Device that will be used is: {0}'.format(device))

    # Load data
    val_transfo = [Normalize(), ToTensor()]
    if data_augmentation:
        train_transfo = [Normalize(), ToPILImage(), RandomRotation(), ToTensor(), GaussianNoise()]
    else:
        train_transfo = val_transfo

    trainset = PCXRayDataset(data_dir, csv_path, splits_path, transform=Compose(train_transfo), pretrained=pretrained,
                             min_patients_per_label=min_patients_per_label)
    trainloader = DataLoader(trainset, batch_size=batch_size, shuffle=True,
                             num_workers=2, pin_memory=True)

    valset = PCXRayDataset(data_dir, csv_path, splits_path, transform=Compose(val_transfo), dataset='val',
                           pretrained=pretrained, min_patients_per_label=min_patients_per_label)
    valloader = DataLoader(valset, batch_size=batch_size, shuffle=True,
                           num_workers=2, pin_memory=True)

    print("{0} patients in training set.".format(len(trainset)))
    print("{0} patients in validation set.".format(len(valset)))

    # Load model
    if pretrained:
        in_channels = 3
    else:
        in_channels = 2 if target == 'joint' else 1
    
    if target == 'joint':
        if joint_model_type == 'concat':
            model = JointConcatModel(num_classes=trainset.nb_labels, in_channels=1)
        elif joint_model_type == 'multitask':
            model = MultiTaskModel(num_classes=trainset.nb_labels, in_channels=1,
                                   combine_at=combine_at, join_how=join_how)
        else:
            model = Hemis(num_classes=trainset.nb_labels, in_channels=1, merge_at=merge_at)
    else:
        model = DenseNet(num_classes=trainset.nb_labels, in_channels=in_channels)

    # Add dropout
    if dropout:
        model = add_dropout(model, p=dropout) if joint_model_type != 'hemis' else add_dropout_hemis(model, p=dropout)

    print(trainset.labels_weights)
    criterion = nn.BCEWithLogitsLoss(pos_weight=trainset.labels_weights.to(device))
    if joint_model_type == 'multitask':
        criterion_L = nn.BCEWithLogitsLoss(pos_weight=trainset.labels_weights.to(device))
        criterion_PA = nn.BCEWithLogitsLoss(pos_weight=trainset.labels_weights.to(device))


    # Optimizer
    optimizer = Adam(model.parameters(), lr=learning_rate, betas=(0.9, 0.999), eps=1e-08, weight_decay=1e-5)
    scheduler = StepLR(optimizer, step_size=10, gamma=0.1)  # Used to decay learning rate

    # Resume training if possible
    start_epoch = 0
    start_batch = 0
    train_loss = []
    val_loss = []
    val_preds_all = []
    val_auc = []
    val_prc = []
    metrics_df = pd.DataFrame(columns=['accuracy', 'auc', 'prc', 'loss', 'epoch', 'error'])
    weights_files = glob(join(output_dir, '{}-e*.pt'.format(target)))  # Find all weights files
    if len(weights_files):
        # Find most recent epoch
        epochs = np.array([int(w[len(join(output_dir, '{}-e'.format(target))):-len('.pt')].split('-')[0]) for w in weights_files])
        start_epoch = epochs.max() + 1
        weights_files = [weights_files[i] for i in np.argwhere(epochs == np.amax(epochs)).flatten()]

        # Find most recent batch
        if len(weights_files) > 1:
            batches = np.array([int(w[len(join(output_dir, '{}-e'.format(target))):-len('.pt')].split('i')[1]) for w in weights_files])
            start_batch = batches.max()
            weights_file = weights_files[np.argmax(batches)]
            start_epoch -= 1
        else:
            weights_file = weights_files[0]
        model.load_state_dict(torch.load(weights_file))

        with open(join(output_dir, '{}-train_loss.pkl'.format(target)), 'rb') as f:
            train_loss = pickle.load(f)

        with open(join(output_dir, '{}-val_preds.pkl'.format(target)), 'rb') as f:
            val_preds_all = pickle.load(f)

        with open(join(output_dir, '{}-val_loss.pkl'.format(target)), 'rb') as f:
            val_loss = pickle.load(f)

        with open(join(output_dir, '{}-val_auc.pkl'.format(target)), 'rb') as f:
            val_auc = pickle.load(f)

        with open(join(output_dir, '{}-val_prc.pkl'.format(target)), 'rb') as f:
            val_prc = pickle.load(f)

        metrics_df = pd.read_csv(join(output_dir, '{}-metrics.csv'.format(target)),
                                 usecols=['accuracy', 'auc', 'prc', 'loss', 'epoch', 'error'], low_memory=False)

        print("Resuming training at epoch {0}.".format(start_epoch))
        print("Weights loaded: {0}".format(weights_file))

    model.to(device)

    # Training loop
    for epoch in range(start_epoch, nb_epoch):  # loop over the dataset multiple times
        scheduler.step()

        model.train()

        running_loss = torch.zeros(1, requires_grad=False, dtype=torch.float).to(device)
        train_preds = []
        train_true = []
        for i, data in enumerate(trainloader, 0):
            # Skip to current batch
            if epoch == start_epoch and i < start_batch:
                continue

            if target == 'pa':
                input, label = data['PA'].to(device), data['encoded_labels'].to(device)
            elif target == 'l':
                input, label = data['L'].to(device), data['encoded_labels'].to(device)
            else:
                pa, l, label = data['PA'].to(device), data['L'].to(device), data['encoded_labels'].to(device)
                input = [pa, l]
            # sample_weights = data['sample_weight'].to(device)

            # Forward
            output = model(input)
            optimizer.zero_grad()
            if joint_model_type == 'multitask':
                joint_logit, frontal_logit, lateral_logit = output
                loss_J = criterion(joint_logit, label)
                loss_PA = criterion_PA(frontal_logit, label) * loss_wts[0]
                loss_L = criterion_L(lateral_logit, label) * loss_wts[1]
                loss = loss_J + loss_L + loss_PA

                output = joint_logit

            else:
                loss = criterion(output, label)
            # loss = (loss * sample_weights / sample_weights.sum()).sum()

            # Backward
            loss.backward()
            optimizer.step()

            # Save predictions
            train_preds.append(torch.sigmoid(output).data.cpu().numpy())
            train_true.append(label.data.cpu().numpy())

            # print statistics
            running_loss += loss.data
            print_every = max(1, len(trainset) // (20 * batch_size))
            if (i + 1) % print_every == 0:
                running_loss = running_loss.cpu().detach().numpy().squeeze() / print_every
                print('[{0}, {1:5}] loss: {2:.5f}'.format(epoch + 1, i + 1, running_loss))
                train_loss.append(running_loss)

                with open(join(output_dir, '{}-train_loss.pkl'.format(target)), 'wb') as f:
                    pickle.dump(train_loss, f)
                torch.save(model.state_dict(), join(output_dir, '{0}-e{1}-i{2}.pt'.format(target, epoch, i + 1)))
                running_loss = torch.zeros(1, requires_grad=False).to(device)

        train_preds = np.vstack(train_preds)
        train_true = np.vstack(train_true)
        train_auc = roc_auc_score(train_true, train_preds, average=None)

        model.eval()

        running_loss = torch.zeros(1, requires_grad=False, dtype=torch.float).to(device)
        val_preds = []
        val_true = []
        for i, data in enumerate(valloader, 0):
            if target == 'pa':
                input, label = data['PA'].to(device), data['encoded_labels'].to(device)
            elif target == 'l':
                input, label = data['L'].to(device), data['encoded_labels'].to(device)
            else:
                pa, l, label = data['PA'].to(device), data['L'].to(device), data['encoded_labels'].to(device)
                input = [pa, l]

            # Forward
            output = model(input)
            if joint_model_type == 'multitask':
                if not vote_at_test:
                    output = output[0]
                else:
                    output = torch.stack(output, dim=1).mean(dim=1)
            running_loss += criterion(output, label).mean().data

            output = torch.sigmoid(output)

            # Save predictions
            val_preds.append(output.data.cpu().numpy())
            val_true.append(label.data.cpu().numpy())

        running_loss = running_loss.cpu().detach().numpy().squeeze() / (len(valset) / batch_size)
        val_loss.append(running_loss)
        print('Epoch {0} - Val loss = {1:.5f}'.format(epoch + 1, running_loss))

        val_preds = np.vstack(val_preds)
        val_true = np.vstack(val_true)
        val_preds_all.append(val_preds)
        auc = roc_auc_score(val_true, val_preds, average=None)
        val_auc.append(auc)
        print("Validation AUC, Train AUC and difference")
        diff_train_val = auc - train_auc
        diff_train_val = np.stack([auc, train_auc, diff_train_val], axis=-1)
        print(diff_train_val.round(4))
        print()

        prc = average_precision_score(val_true, val_preds, average=None)
        val_prc.append(prc)

        metrics = {'accuracy': accuracy_score(val_true, np.where(val_preds > 0.5, 1, 0)),
                   'auc': roc_auc_score(val_true, val_preds, average='weighted'),
                   'prc': average_precision_score(val_true, val_preds, average='weighted'),
                   'loss': running_loss, 'epoch': epoch + 1}
        metrics_df = metrics_df.append(metrics, ignore_index=True)
        print(metrics)

        with open(join(output_dir, '{}-val_preds.pkl'.format(target)), 'wb') as f:
            pickle.dump(val_preds_all, f)

        with open(join(output_dir, '{}-val_loss.pkl'.format(target)), 'wb') as f:
            pickle.dump(val_loss, f)

        with open(join(output_dir, '{}-val_auc.pkl'.format(target)), 'wb') as f:
            pickle.dump(val_auc, f)

        with open(join(output_dir, '{}-val_prc.pkl'.format(target)), 'wb') as f:
            pickle.dump(val_prc, f)

        metrics_df.to_csv(join(output_dir, '{}-metrics.csv'.format(target)))

        torch.save(model.state_dict(), join(output_dir, '{}-e{}.pt'.format(target, epoch)))

        # Remove all batches weights
        weights_files = glob(join(output_dir, '{}-e{}-i*.pt'.format(target, epoch)))
        for file in weights_files:
            os.remove(file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Usage')
    parser.add_argument('data_dir', type=str)
    parser.add_argument('csv_path', type=str)
    parser.add_argument('splits_path', type=str)
    parser.add_argument('output_dir', type=str)
    parser.add_argument('--target', type=str, default='pa')
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--pretrained', type=bool, default=False)
    parser.add_argument('--learning_rate', type=float, default=0.0001)
    parser.add_argument('--dropout', type=float, default=0.2)
    parser.add_argument('--min_patients', type=int, default=50)
    parser.add_argument('--seed', type=int, default=666)
    parser.add_argument('--merge', type=int, default=2)
    parser.add_argument('--jointmodel', type=str, default='hemis')
    parser.add_argument('--mt-combine-at', dest='combine', type=str, default='prepool')
    parser.add_argument('--mt-join', dest='join', type=str, default='concat')
    parser.add_argument('--loss-weights', type=str, default='0.3,0.3')

    args = parser.parse_args()
    np.set_printoptions(suppress=True, precision=4)
    multitask_loss_weights = [float(x) for x in args.loss_weights.split(",")]
    print(args)
    train(args.data_dir, args.csv_path, args.splits_path, args.output_dir, target=args.target,
          batch_size=args.batch_size, nb_epoch=args.epochs, pretrained=args.pretrained,
          learning_rate=args.learning_rate, min_patients_per_label=args.min_patients,
          dropout=args.dropout, seed=args.seed, joint_model_type=args.jointmodel,
          combine_at=args.combine, join_how=args.join, merge_at=args.merge, loss_wts=multitask_loss_weights)
