from typing import Any, List

import timm
import torch
import torch.nn as nn
import torch.nn.functional as F

# from fairscale.nn import auto_wrap, checkpoint_wrapper, wrap
from lightning.pytorch import LightningModule
from torchmetrics import MaxMetric, MeanMetric
from torchmetrics.classification.accuracy import Accuracy
from torchvision import transforms as T


def create_model(model, input_ch=3, num_cls=10):
    """Load a model from timm."""
    timm_model = timm.models.create_model(
        model, pretrained=True, in_chans=input_ch, num_classes=num_cls
    )
    return timm_model


class CIFAR10LitModule(LightningModule):
    """Example of LightningModule for CIFAR10 classification.

    A LightningModule organizes your PyTorch code into 6 sections:
        - Computations (init)
        - Train loop (training_step)
        - Validation loop (validation_step)
        - Test loop (test_step)
        - Prediction Loop (predict_step)
        - Optimizers and LR Schedulers (configure_optimizers)

    Docs:
        https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html
    """

    def __init__(
        self,
        net: timm.models,
        optimizer: torch.optim.Optimizer,
    ):
        super().__init__()

        # this line allows to access init params with 'self.hparams' attribute
        # also ensures init params will be stored in ckpt
        self.save_hyperparameters(logger=False, ignore=["net"])

        self.net = net

        # loss function
        self.criterion = torch.nn.CrossEntropyLoss()

        # metric objects for calculating and averaging accuracy across batches
        self.train_acc = Accuracy(task="multiclass", num_classes=10)
        self.val_acc = Accuracy(task="multiclass", num_classes=10)
        self.test_acc = Accuracy(task="multiclass", num_classes=10)

        # for averaging loss across batches
        self.train_loss = MeanMetric()
        self.val_loss = MeanMetric()
        self.test_loss = MeanMetric()

        # for tracking best so far validation accuracy
        self.val_acc_best = MaxMetric()

        self.predict_transform = torch.nn.Sequential(
            T.Resize([224, 224]),
            T.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            # T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
        )

    # def configure_sharded_model(self):
    #     self.net = auto_wrap(self.net)

    def forward(self, x: torch.Tensor):
        return self.net(x)

    # @torch.jit.export
    def pass_jit(self, x: torch.Tensor):
        with torch.no_grad():
            # transform the inputs
            x = self.predict_transform(x)
            # forward pass
            logits = self(x)
            preds = F.softmax(logits, dim=-1)

        return preds

    def on_train_start(self):
        # by default lightning executes validation step sanity checks before training starts,
        # so we need to make sure val_acc_best doesn't store accuracy from these checks
        self.val_acc_best.reset()

    def step(self, batch: Any):
        x, y = batch
        logits = self.forward(x)
        loss = self.criterion(logits, y)
        preds = torch.argmax(logits, dim=1)
        return loss, preds, y

    def training_step(self, batch: Any, batch_idx: int):
        loss, preds, targets = self.step(batch)

        # update and log metrics
        self.train_loss(loss)
        self.train_acc(preds, targets)
        self.log(
            "train/loss",
            self.train_loss,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            sync_dist=True,
        )
        self.log(
            "train/acc", self.train_acc, on_step=True, on_epoch=True, prog_bar=True, sync_dist=True
        )

        # we can return here dict with any tensors
        # and then read it in some callback or in `training_epoch_end()` below
        # remember to always return loss from `training_step()` or backpropagation will fail!
        return {"loss": loss, "preds": preds, "targets": targets}

    def on_train_epoch_end(self):
        # `outputs` is a list of dicts returned from `training_step()`
        pass

    def validation_step(self, batch: Any, batch_idx: int):
        loss, preds, targets = self.step(batch)

        # update and log metrics
        self.val_loss(loss)
        self.val_acc(preds, targets)
        self.log(
            "val/loss", self.val_loss, on_step=False, on_epoch=True, prog_bar=True, sync_dist=True
        )
        self.log(
            "val/acc", self.val_acc, on_step=False, on_epoch=True, prog_bar=True, sync_dist=True
        )
        self.log("hp_metric", self.val_loss, sync_dist=True)

        return {"loss": loss, "preds": preds, "targets": targets}

    def on_validation_epoch_end(self):
        acc = self.val_acc.compute()  # get current val acc
        self.val_acc_best(acc)  # update best so far val acc
        # log `val_acc_best` as a value through `.compute()` method, instead of as a metric object
        # otherwise metric would be reset by lightning after each epoch
        self.log("val/acc_best", self.val_acc_best.compute(), prog_bar=True, sync_dist=True)

    def test_step(self, batch: Any, batch_idx: int):
        loss, preds, targets = self.step(batch)

        # update and log metrics
        self.test_loss(loss)
        self.test_acc(preds, targets)
        self.log(
            "test/loss",
            self.test_loss,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            sync_dist=True,
        )
        self.log(
            "test/acc", self.test_acc, on_step=False, on_epoch=True, prog_bar=True, sync_dist=True
        )

        return {"loss": loss, "preds": preds, "targets": targets}

    def on_test_epoch_end(self):
        pass

    def configure_optimizers(self):
        """Choose what optimizers and learning-rate schedulers to use in your optimization.
        Normally you'd need one. But in the case of GANs or similar you might have multiple.

        Examples:
            https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#configure-optimizers
        """
        return {
            "optimizer": self.hparams.optimizer(params=self.parameters()),
        }


if __name__ == "__main__":
    import hydra
    import omegaconf
    import pyrootutils

    root = pyrootutils.setup_root(__file__, pythonpath=True)
    cfg = omegaconf.OmegaConf.load(root / "configs" / "model" / "timm_cifar10.yaml")
    _ = hydra.utils.instantiate(cfg)