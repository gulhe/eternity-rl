import os
from pathlib import Path

import hydra
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
import torch.optim as optim
from hydra.utils import to_absolute_path
from omegaconf import DictConfig, OmegaConf
from torch.nn.parallel import DistributedDataParallel as DDP
from torch_optimizer import Lamb

from src.environment import BatchedEternityEnv
from src.model import CNNPolicy
from src.reinforce import Reinforce


def setup_distributed(rank: int, world_size: int):
    """Setup distributed training."""
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "12355"

    # Initialize the process group.
    dist.init_process_group("nccl", rank=rank, world_size=world_size)


def cleanup_distributed():
    """Cleanup distributed training."""
    dist.destroy_process_group()


def init_env(config: DictConfig) -> BatchedEternityEnv:
    """Initialize the environment."""
    env = BatchedEternityEnv.from_file(
        config.env.path,
        config.rollout_buffer.buffer_size,
        config.env.reward,
        config.env.max_steps,
        config.device,
        config.seed,
    )
    return env


def init_model(config: DictConfig, env: BatchedEternityEnv) -> CNNPolicy:
    """Initialize the model."""
    model = CNNPolicy(
        n_classes=env.n_classes,
        embedding_dim=config.model.embedding_dim,
        n_res_layers=config.model.n_res_layers,
        n_mlp_layers=config.model.n_gru_layers,
        n_head_layers=config.model.n_head_layers,
        maxpool_kernel=config.model.maxpool_kernel,
        board_width=env.board_size,
        board_height=env.board_size,
        zero_init_residuals=config.model.zero_init_residuals,
        use_time_embedding=config.model.use_time_embedding,
    )
    return model


def init_optimizer(config: DictConfig, model: nn.Module) -> optim.Optimizer:
    """Initialize the optimizer."""
    optimizer_name = config.optimizer.optimizer
    lr = config.optimizer.learning_rate
    weight_decay = config.optimizer.weight_decay
    optimizers = {
        "adamw": optim.AdamW,
        "adam": optim.Adam,
        "sgd": optim.SGD,
        "rmsprop": optim.RMSprop,
        "lamb": Lamb,
    }

    if optimizer_name not in optimizers:
        print(f"Unknown optimizer: {optimizer_name}.")
        print("Using AdamW instead.")
        optimizer_name = "adamw"

    optimizer = optimizers[optimizer_name](
        model.parameters(), lr=lr, weight_decay=weight_decay
    )

    return optimizer


def init_scheduler(
    config: DictConfig, optimizer: optim.Optimizer
) -> optim.lr_scheduler.LinearLR:
    """Initialize the scheduler."""
    scheduler = optim.lr_scheduler.LinearLR(
        optimizer=optimizer,
        start_factor=0.001,
        end_factor=1.0,
        total_iters=config.scheduler.warmup_steps,
    )
    return scheduler


def init_trainer(
    config: DictConfig,
    env: BatchedEternityEnv,
    model: nn.Module,
    optimizer: optim.Optimizer,
    scheduler: optim.lr_scheduler.LinearLR,
) -> Reinforce:
    """Initialize the trainer."""
    trainer = Reinforce(
        env,
        model,
        optimizer,
        scheduler,
        config.device,
        config.reinforce.entropy_weight,
        config.reinforce.gamma,
        config.reinforce.clip_value,
        config.rollout_buffer.batch_size,
        config.rollout_buffer.batches_per_rollouts,
        config.reinforce.total_rollouts,
        config.reinforce.advantage,
        config.reinforce.save_every,
    )
    return trainer


def run_trainer_ddp(rank: int, world_size: int, config: DictConfig):
    """Run the trainer in distributed mode."""
    setup_distributed(rank, world_size)

    # Make sure we log training info only for the rank 0 process.
    if rank != 0:
        config.mode = "disabled"

    config.device = config.distributed[rank]
    if config.device == "auto":
        config.device = "cuda" if torch.cuda.is_available() else "cpu"

    env = init_env(config)
    model = init_model(config, env)
    model = model.to(config.device)
    model = DDP(model, device_ids=[config.device], output_device=config.device)
    optimizer = init_optimizer(config, model)
    scheduler = init_scheduler(config, optimizer)
    trainer = init_trainer(config, env, model, optimizer, scheduler)

    try:
        trainer.launch_training(
            config.group, OmegaConf.to_container(config), config.mode
        )
    except KeyboardInterrupt:
        # Capture a potential ctrl+c to make sure we clean up distributed processes.
        print("Caught KeyboardInterrupt. Cleaning up distributed processes...")
    finally:
        cleanup_distributed()


def run_trainer_single_gpu(config: DictConfig):
    """Run the trainer in single GPU or CPU mode."""
    if config.device == "auto":
        config.device = "cuda" if torch.cuda.is_available() else "cpu"

    env = init_env(config)
    model = init_model(config, env)
    model = model.to(config.device)
    optimizer = init_optimizer(config, model)
    scheduler = init_scheduler(config, optimizer)
    trainer = init_trainer(config, env, model, optimizer, scheduler)

    trainer.launch_training(config.group, OmegaConf.to_container(config), config.mode)


@hydra.main(version_base="1.3", config_path="configs", config_name="default")
def main(config: DictConfig):
    config.env.path = Path(to_absolute_path(config.env.path))
    world_size = len(config.distributed)
    if world_size > 1:
        mp.spawn(run_trainer_ddp, nprocs=world_size, args=(world_size, config))
    else:
        run_trainer_single_gpu(config)


if __name__ == "__main__":
    # Launch with hydra.
    main()
