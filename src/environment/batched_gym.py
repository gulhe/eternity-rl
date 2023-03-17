"""A batched version of the environment.
All actions are made on the batch.
"""
from typing import Any

import gymnasium as gym
import gymnasium.spaces as spaces
import numpy as np
import torch
from einops import rearrange, repeat

from .gym import EAST, NORTH, SOUTH, WEST

# Defines convs that will compute vertical and horizontal matches.
# Shapes are [out_channels, in_channels, kernel_height, kernel_width].
# See `BatchedEternityEnv.matches` for more information.
# Don't forget that the y-axis is reversed!!
HORIZONTAL_CONV = torch.zeros((1, 4, 2, 1))
HORIZONTAL_CONV[0, SOUTH, 1, 0] = 1
HORIZONTAL_CONV[0, NORTH, 0, 0] = -1

VERTICAL_CONV = torch.zeros((1, 4, 1, 2))
VERTICAL_CONV[0, EAST, 0, 0] = 1
VERTICAL_CONV[0, WEST, 0, 1] = -1

# Convs to detect the 0-0 matches.
HORIZONTAL_ZERO_CONV = torch.zeros((1, 4, 2, 1))
HORIZONTAL_ZERO_CONV[0, SOUTH, 1, 0] = 1
HORIZONTAL_ZERO_CONV[0, NORTH, 0, 0] = 1

VERTICAL_ZERO_CONV = torch.zeros((1, 4, 1, 2))
VERTICAL_ZERO_CONV[0, EAST, 0, 0] = 1
VERTICAL_ZERO_CONV[0, WEST, 0, 1] = 1


class BatchedEternityEnv(gym.Env):
    """A batched version of the environment.
    All computations are done on GPU using torch tensors
    instead of numpy arrays.
    """

    metadata = {"render.modes": ["computer"]}

    def __init__(self, batch_instances: torch.Tensor, device: str, seed: int = 0):
        """Initialize the environment.

        ---
        Args:
            batch_instances: The instances of this environment.
                Long tensor of shape of [batch_size, 4, size, size].
            seed: The seed for the random number generator.
        """
        assert len(batch_instances.shape) == 4, "Tensor must have 4 dimensions."
        assert batch_instances.shape[1] == 4, "The pieces must have 4 sides."
        assert (
            batch_instances.shape[2] == batch_instances.shape[3]
        ), "Instances are not squares."
        assert torch.all(batch_instances >= 0), "Classes must be positives."

        super().__init__()
        self.instances = batch_instances.to(device)
        self.device = device
        self.rng = torch.Generator(device).manual_seed(seed)

        # Instances infos.
        self.size = self.instances.shape[-1]
        self.n_pieces = self.size * self.size
        self.n_class = self.instances.max().cpu().item() + 1
        self.max_steps = self.n_pieces
        self.best_matches = 2 * self.size * (self.size - 1)
        self.batch_size = self.instances.shape[0]

        # Dynamic infos.
        self.step_id = 0
        self.truncated = False
        self.terminated = torch.zeros(self.batch_size, dtype=torch.bool, device=device)

        # Spaces
        # Those spaces do not take into account that
        # this env is a batch of multiple instances.
        self.action_space = spaces.MultiDiscrete(
            [
                self.n_pieces,  # Tile id to swap.
                self.n_pieces,  # Tile id to swap.
                4,  # How much rolls for the first tile.
                4,  # How much rolls for the first tile.
            ]
        )
        self.observation_space = spaces.Box(
            low=0, high=1, shape=self.instances.shape[1:], dtype=np.uint8
        )

    def reset(self) -> tuple[torch.Tensor, dict[str, Any]]:
        """Reset the environment.

        Scrambles the instances and reset their infos.
        """
        self.scramble_instances()
        self.step_id = 0
        self.truncated = False
        self.terminated = torch.zeros(
            self.batch_size, dtype=torch.bool, device=self.device
        )

        return self.render(), dict()

    def step(
        self, actions: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, bool, dict[str, Any]]:
        """Do a batched step through all instances.

        ---
        Args:
            actions: Batch of actions to apply.
                Long tensor of shape of [batch_size, tile_id_1, shift_1, tile_id_2, shift_2].

        ---
        Returns:
            observations: The observation of the environments.
                Shape of [batch_size, 4, size, size].
            rewards: The reward of the environments.
                Shape of [batch_size,].
            terminated: Whether the environments are terminated (won).
                Shape of [batch_size,].
            truncated: Whether the environments are truncated (max steps reached).
                Shape of [batch_size,].
            infos: Additional infos.
        """
        self.step_id += 1
        tiles_id_1, tiles_id_2 = actions[:, 0], actions[:, 2]
        shifts_1, shifts_2 = actions[:, 1], actions[:, 3]

        matches = self.matches

        self.roll_tiles(tiles_id_1, shifts_1)
        self.roll_tiles(tiles_id_2, shifts_2)
        self.swap_tiles(tiles_id_1, tiles_id_2)

        # Maintain the previous terminated states.
        self.terminated |= self.matches == self.best_matches
        self.truncated = self.step_id >= self.max_steps

        # Only give a reward at the end of the episode.
        # if not self.truncated:
        #     rewards = self.matches * self.terminated / self.best_matches
        # else:
        #     rewards = self.matches / self.best_matches
        rewards = (self.matches - matches) / self.best_matches

        return (
            self.render(),
            rewards,
            self.terminated,
            self.truncated,
            dict(),
        )

    def roll_tiles(self, tile_ids: torch.Tensor, shifts: torch.Tensor):
        """Rolls tiles at the given ids for the given shifts.
        It actually shifts all tiles, but with 0-shifts
        except for the pointed tile ids.

        ---
        Args:
            tile_ids: The id of the tiles to roll.
                Shape of [batch_size,].
            shifts: The number of shifts for each tile.
                Shape of [batch_size,].
        """
        total_shifts = torch.zeros(
            self.batch_size * self.n_pieces, dtype=torch.long, device=self.device
        )
        offsets = torch.arange(
            0, self.batch_size * self.n_pieces, self.n_pieces, device=self.device
        )
        total_shifts[tile_ids + offsets] = shifts

        self.instances = rearrange(self.instances, "b c h w -> (b h w) c")
        self.instances = self.batched_roll(self.instances, total_shifts)
        self.instances = rearrange(
            self.instances,
            "(b h w) c -> b c h w",
            b=self.batch_size,
            h=self.size,
            w=self.size,
        )

    def swap_tiles(self, tile_ids_1: torch.Tensor, tile_ids_2: torch.Tensor):
        """Swap two tiles in each element of the batch.

        ---
        Args:
            tile_ids_1: The id of the first tiles to swap.
                Shape of [batch_size,].
            tile_ids_2: The id of the second tiles to swap.
                Shape of [batch_size,].
        """
        offsets = torch.arange(
            0, self.batch_size * self.n_pieces, self.n_pieces, device=self.device
        )
        tile_ids_1 = tile_ids_1 + offsets
        tile_ids_2 = tile_ids_2 + offsets
        self.instances = rearrange(self.instances, "b c h w -> (b h w) c")
        self.instances[tile_ids_1], self.instances[tile_ids_2] = (
            self.instances[tile_ids_2],
            self.instances[tile_ids_1],
        )
        self.instances = rearrange(
            self.instances,
            "(b h w) c -> b c h w",
            b=self.batch_size,
            h=self.size,
            w=self.size,
        )

    @property
    def matches(self) -> torch.Tensor:
        """The number of matches for each instance.
        Uses convolutions to vectorize the computations.

        The main idea is to compute the sum $class_id_1 - class_id_2$,
        where "1" and "2" represents two neighbour tiles.
        If this sum equals to 0, then the class_id are the same.

        We still have to make sure that we're not computing 0-0 matchings,
        so we also compute $class_id_1 + class_id_2$ and check if this is equal
        to 0 (which would mean that both class_id are equal to 0).
        """
        n_matches = torch.zeros(self.batch_size, device=self.device)

        for conv in [HORIZONTAL_CONV, VERTICAL_CONV]:
            res = torch.conv2d(self.instances.float(), conv.to(self.device))
            n_matches += (res == 0).float().flatten(start_dim=1).sum(dim=1)

        # Remove the 0-0 matches from the count.
        for conv in [HORIZONTAL_ZERO_CONV, VERTICAL_ZERO_CONV]:
            res = torch.conv2d(self.instances.float(), conv.to(self.device))
            n_matches -= (res == 0).float().flatten(start_dim=1).sum(dim=1)

        return n_matches.long()

    def scramble_instances(self):
        """Scrambles the instances to start from a new valid configuration."""
        # Scrambles the tiles.
        self.instances = rearrange(
            self.instances, "b c h w -> b (h w) c", w=self.size
        )  # Shape of [batch_size, n_pieces, 4].
        permutations = torch.stack(
            [
                torch.randperm(self.n_pieces, generator=self.rng, device=self.device)
                for _ in range(self.batch_size)
            ]
        )  # Shape of [batch_size, n_pieces].
        # Permute tiles according to `permutations`.
        for instance_id in range(self.batch_size):
            self.instances[instance_id] = self.instances[instance_id][
                permutations[instance_id]
            ]

        # Randomly rolls the tiles.
        shifts = torch.randint(
            low=0,
            high=4,
            size=(self.batch_size * self.n_pieces,),
            generator=self.rng,
            device=self.device,
        )
        self.instances = rearrange(self.instances, "b p c -> (b p) c")
        self.instances = self.batched_roll(self.instances, shifts)
        self.instances = rearrange(
            self.instances,
            "(b h w) c -> b c h w",
            b=self.batch_size,
            h=self.size,
            w=self.size,
        )

    def render(self, mode: str = "computer") -> torch.Tensor:
        """Render the environment.

        ---
        Args:
            mode: The rendering type.

        ---
        Returns:
            The observation of shape [batch_size, 4, size, size].
        """
        match mode:
            case "computer":
                return self.instances
            case _:
                raise RuntimeError(f"Unknown rendering type: {mode}.")

    @staticmethod
    def batched_roll(input_tensor: torch.Tensor, shifts: torch.Tensor) -> torch.Tensor:
        """Batched version of `torch.roll`.
        It applies a circular shifts to the last dimension of the tensor.

        ---
        Args:
            input_tensor: The tensor to roll.
                Shape of [batch_size, hidden_size].
            shifts: The number of shifts for each element of the batch.
                Shape of [batch_size,].

        ---
        Returns:
            The rolled tensor.
        """
        batch_size, hidden_size = input_tensor.shape
        # In the right range of values (circular shift).
        shifts = shifts % hidden_size
        # To match the same direction as `torch.roll`.
        shifts = hidden_size - shifts

        # Compute the indices that will select the right part of the extended tensor.
        offsets = torch.arange(hidden_size, device=input_tensor.device)
        offsets = repeat(offsets, "h -> b h", b=batch_size)
        select_indices = shifts.unsqueeze(1) + offsets

        # Extend the input tensor with circular padding.
        input_tensor = torch.concat((input_tensor, input_tensor), dim=1)

        rolled_tensor = torch.gather(input_tensor, dim=1, index=select_indices)
        return rolled_tensor
