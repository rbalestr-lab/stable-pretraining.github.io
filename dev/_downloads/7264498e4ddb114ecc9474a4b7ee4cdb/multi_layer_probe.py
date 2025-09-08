"""Multi-layer probe for vision models."""

import argparse
from typing import Dict, List, Tuple

import hydra
import lightning as pl
import torch
import torchmetrics
import torchvision
from datasets import load_dataset
from lightning.pytorch.callbacks import ModelCheckpoint
from lightning.pytorch.loggers import WandbLogger  # type: ignore
from omegaconf import DictConfig
from torch import nn
from transformers import (
    AutoImageProcessor,
    AutoModel,
    AutoModelForZeroShotImageClassification,
    AutoProcessor,
)

import stable_pretraining as spt
from stable_pretraining.data import transforms

# -----------------------------
# Model registry
# -----------------------------
MODEL_ZOO = {
    "DINOv2": {
        "processor_cls": AutoImageProcessor,
        "processor_name": "facebook/dinov2-base",
        "model_cls": AutoModel,
        "model_name": "facebook/dinov2-base",
        "pooling": "cls",
        "probe_skip": 1,
    },
    "DINOv3": {
        "processor_cls": AutoImageProcessor,
        "processor_name": "facebook/dinov3-vitb16-pretrain-lvd1689m",
        "model_cls": AutoModel,
        "model_name": "facebook/dinov3-vitb16-pretrain-lvd1689m",
        "pooling": "cls",
        "probe_skip": 1,
    },
    "MetaCLIP": {
        "processor_cls": AutoProcessor,
        "processor_name": "facebook/metaclip-b16-400m",
        "model_cls": AutoModelForZeroShotImageClassification,
        "model_name": "facebook/metaclip-b16-400m",
        "pooling": "mean",
        "probe_skip": 1,
    },
    "IJEPA-1k": {
        "processor_cls": AutoImageProcessor,
        "processor_name": "facebook/ijepa_vith14_1k",
        "model_cls": AutoModel,
        "model_name": "facebook/ijepa_vith14_1k",
        "pooling": "mean",
        "probe_skip": 1,
    },
    "IJEPA-22k": {
        "processor_cls": AutoImageProcessor,
        "processor_name": "facebook/ijepa_vith14_22k",
        "model_cls": AutoModel,
        "model_name": "facebook/ijepa_vith14_22k",
        "pooling": "mean",
        "probe_skip": 1,
    },
}


# -----------------------------
# Utilities
# -----------------------------


def build_datasets() -> Tuple[torch.utils.data.Dataset, torch.utils.data.Dataset]:
    # Load the Hugging Face dataset
    train_dataset = spt.data.HFDataset(
        "clane9/imagenet-100",
        split="train",
        transform=transforms.RGB(),
    )

    val_dataset = spt.data.HFDataset(
        "clane9/imagenet-100",
        split="validation",
        transform=transforms.RGB(),
    )

    return train_dataset, val_dataset


def make_collate_fn(processor):
    def collate_fn(examples):
        images = [ex["image"] for ex in examples]
        labels = torch.tensor([ex["label"] for ex in examples], dtype=torch.long)
        batch = processor(images=images, return_tensors="pt")
        return {"images": batch, "label": labels}

    return collate_fn


def build_dataloaders(
    train_dataset,
    val_dataset,
    processor,
    batch_size: int = 128,
    num_workers: int = 6,
) -> Tuple[torch.utils.data.DataLoader, torch.utils.data.DataLoader]:
    train_loader = torch.utils.data.DataLoader(
        dataset=train_dataset,
        sampler=spt.data.sampler.RepeatedRandomSampler(train_dataset),
        batch_size=batch_size,
        num_workers=num_workers,
        drop_last=True,
        pin_memory=True,
        collate_fn=make_collate_fn(processor),
    )

    val_loader = torch.utils.data.DataLoader(
        dataset=val_dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        pin_memory=True,
        collate_fn=make_collate_fn(processor),
    )
    return train_loader, val_loader


def load_backbone(model_name: str):
    spec = MODEL_ZOO[model_name]
    processor = spec["processor_cls"].from_pretrained(spec["processor_name"])  # type: ignore
    model = spec["model_cls"].from_pretrained(
        spec["model_name"], output_hidden_states=True
    )  # type: ignore

    config = model.config if "CLIP" not in model_name else model.config.vision_config
    emb_dim = config.hidden_size
    num_hidden_layers = config.num_hidden_layers
    pooling = spec["pooling"]
    probe_skip = spec.get("probe_skip", 1)

    if "CLIP" in model_name:
        model = model.vision_model

    return model, processor, emb_dim, num_hidden_layers, pooling, probe_skip


# -----------------------------
# Lightning-compatible `spt.Module`
# -----------------------------


def build_module(
    model, processor, transformer_block_indices: List[int], pooling: str
) -> spt.Module:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Define the forward used by `spt.Module`
    def forward(self, batch: Dict, stage: str):  # noqa: ARG001 (stage provided by spt)
        out: Dict[str, torch.Tensor] = {}

        # Preprocess & move to device
        # images = processor(batch["image"], return_tensors="pt")
        images = {
            k: v.to(device=device, non_blocking=True)
            for k, v in batch["images"].items()
        }

        outputs = self.model(**images, output_hidden_states=True)
        hiddens = outputs["hidden_states"]  # tuple: [embeddings, block1, block2, ...]

        # Mean-pool tokens per layer -> (B, D)
        for i in transformer_block_indices:
            x = hiddens[1 + i]

            if pooling == "cls":
                x = x[:, 0]
            elif pooling == "mean":
                x = x.mean(dim=1)
            else:
                raise ValueError(f"Unknown pooling type: {pooling}")

            out[f"embedding_layer_{i}"] = x.detach()
        return out

    module = spt.Module(
        model=model,  # spt.backbone.EvalOnly(model),  # freeze eval-only backbone
        forward=forward,
        processor=processor,
        optim=None,  # probes have their own optimizers
    )
    return module


# -----------------------------
# Probes
# -----------------------------


def build_probes(emb_dim: int, num_classes: int, transformer_block_indices: List[int]):
    probes = []
    for i in transformer_block_indices:
        probes.append(
            spt.callbacks.OnlineProbe(
                target="label",
                name=f"linear_probe_block_{i}",
                input=f"embedding_layer_{i}",
                probe=nn.Sequential(
                    nn.BatchNorm1d(emb_dim),
                    nn.Linear(emb_dim, num_classes),
                ),
                loss_fn=nn.CrossEntropyLoss(),
                metrics={
                    "top1": torchmetrics.classification.MulticlassAccuracy(num_classes),
                    "top5": torchmetrics.classification.MulticlassAccuracy(
                        num_classes, top_k=5
                    ),
                },
                optimizer={"type": "SGD", "lr": 1e-3},
                scheduler={"type": "CosineAnnealingLR", "T_max": 100},
            )
        )
    return probes


# -----------------------------
# Main
# -----------------------------


@hydra.main(config_path="config_examples", config_name="multi_probe")
def main(cfg: DictConfig):
    pl.seed_everything(cfg.seed, workers=True)

    # Backbone & module
    model, processor, emb_dim, num_layers, pooling, probe_skip = load_backbone(
        cfg.model
    )
    # Most ViT-like models have 12 blocks; adapt as needed
    transformer_block_indices = list(range(0, num_layers, probe_skip))
    module = build_module(model, processor, transformer_block_indices, pooling)

    # Data
    train_ds, val_ds = build_datasets()
    train_loader, val_loader = build_dataloaders(
        train_ds,
        val_ds,
        processor,
        batch_size=cfg.batch_size,
        num_workers=cfg.num_workers,
    )
    data = spt.data.DataModule(train=train_loader, val=val_loader)

    # Probes
    probes = build_probes(
        emb_dim=emb_dim,
        num_classes=100,
        transformer_block_indices=transformer_block_indices,
    )

    # Trainer
    precision = "16-mixed" if torch.cuda.is_available() else 32
    logger = None
    if cfg.use_wandb and WandbLogger is not None:
        logger = WandbLogger(project=cfg.project)

    checkpoint_callback = ModelCheckpoint(filename="ckpt", save_last=True)

    trainer = pl.Trainer(
        max_epochs=cfg.epochs,
        callbacks=probes + [checkpoint_callback],
        precision=precision,
        logger=logger,
        enable_checkpointing=True,
    )

    # Run
    manager = spt.Manager(trainer=trainer, module=module, data=data)
    manager()


if __name__ == "__main__":
    main()
