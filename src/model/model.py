from typing import Optional

import numpy as np
import torch
import torch.nn as nn
from einops import rearrange, repeat
from torch.distributions import Categorical
from torchinfo import summary

from .backbone import Backbone
from .heads import SelectSide, SelectTile

N_SIDES, N_ACTIONS = 4, 4


class Policy(nn.Module):
    def __init__(
        self,
        n_classes: int,
        board_width: int,
        board_height: int,
        n_channels: int,
        embedding_dim: int,
        n_heads: int,
        backbone_cnn_layers: int,
        backbone_transformer_layers: int,
        decoder_layers: int,
        dropout: float,
    ):
        super().__init__()
        self.board_width = board_width
        self.board_height = board_height
        self.embedding_dim = embedding_dim

        self.backbone = Backbone(
            n_classes,
            n_channels,
            embedding_dim,
            n_heads,
            backbone_cnn_layers,
            backbone_transformer_layers,
            dropout,
        )

        self.select_tile = SelectTile(embedding_dim, n_heads, decoder_layers, dropout)
        self.select_side = SelectSide(
            embedding_dim, n_heads, decoder_layers, dropout, N_SIDES
        )
        self.node_query = nn.Parameter(torch.randn(embedding_dim))
        self.side_query = nn.Parameter(torch.randn(embedding_dim))
        self.node_selected_embeddings = nn.Parameter(torch.randn(2, embedding_dim))
        self.side_embeddings = nn.Parameter(torch.randn(N_SIDES, embedding_dim))

    def dummy_input(self, device: str) -> tuple[torch.Tensor, torch.Tensor]:
        tiles = torch.zeros(
            1,
            N_SIDES,
            self.board_height,
            self.board_width,
            dtype=torch.long,
            device=device,
        )
        hidden_state = torch.zeros(
            1,
            self.embedding_dim,
            dtype=torch.float,
            device=device,
        )
        return tiles, hidden_state

    def summary(self, device: str):
        """Torchinfo summary."""
        dummy_input = self.dummy_input(device)
        summary(
            self,
            input_data=[*dummy_input],
            depth=1,
            device=device,
        )

    def forward(
        self,
        tiles: torch.Tensor,
        hidden_state: Optional[torch.Tensor] = None,
        sampling_mode: str = "sample",
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Predict the actions and value for the given game states.

        ---
        Args:
            tiles: The game state.
                Tensor of shape [batch_size, 4, board_height, board_width].
            hidden_state: The previous hidden state of the model.
                Initialized to 0 if not provided.
                Tensor of shape [batch_size, embedding_dim].
            sampling_mode: The sampling mode of the actions.

        ---
        Returns:
            actions: The predicted actions.
                Shape of [batch_size, n_actions].
            logprobs: The log probabilities of the predicted actions.
                Shape of [batch_size, n_actions].
            entropies: The entropies of the predicted actions.
                Shape of [batch_size, n_actions].
        """
        batch_size = tiles.shape[0]
        if hidden_state is None:
            hidden_state = torch.zeros(
                (batch_size, self.embedding_dim),
                dtype=torch.float,
                device=tiles.device,
            )

        tiles, hidden_state = self.backbone(tiles, hidden_state)

        actions, logprobs, entropies = [], [], []

        # Node selections.
        for node_number in range(2):
            queries = repeat(self.node_query, "e -> b e", b=batch_size)
            probs = self.select_tile(tiles, queries)

            sampled_nodes = self.sample_actions(probs, sampling_mode)
            actions_logprob, actions_entropy = Policy.logprobs(probs, sampled_nodes)

            actions.append(sampled_nodes)
            logprobs.append(actions_logprob)
            entropies.append(actions_entropy)

            selected_embedding = self.node_selected_embeddings[node_number]
            selected_embedding = repeat(selected_embedding, "e -> b e", b=batch_size)
            tiles = Policy.selective_add(tiles, selected_embedding, sampled_nodes)

        # Side selections.
        for _ in range(2):
            queries = repeat(self.side_query, "e -> b e", b=batch_size)
            probs = self.select_side(tiles, queries)

            sampled_sides = self.sample_actions(probs, sampling_mode)
            actions_logprob, actions_entropy = Policy.logprobs(probs, sampled_sides)

            actions.append(sampled_sides)
            logprobs.append(actions_logprob)
            entropies.append(actions_entropy)

            side_embedding = self.side_embeddings[sampled_sides]
            tiles = Policy.selective_add(tiles, side_embedding, sampled_sides)

        actions = torch.stack(actions, dim=1)
        logprobs = torch.stack(logprobs, dim=1)
        entropies = torch.stack(entropies, dim=1)

        return actions, logprobs, entropies, hidden_state

    @staticmethod
    def sample_actions(probs: torch.Tensor, mode: str) -> torch.Tensor:
        match mode:
            case "sample":
                categorical = Categorical(probs=probs)
                action_ids = categorical.sample()
            case "argmax":
                action_ids = torch.argmax(probs, dim=-1)
            case _:
                raise ValueError(f"Invalid mode: {mode}")
        return action_ids

    @staticmethod
    def logprobs(
        probs: torch.Tensor,
        action_ids: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Sample actions from the given probabilities.
        Returns the sampled actions and their log-probilities.

        ---
        Args:
            probs: Probabilities of the actions.
                Shape of [batch_size, n_actions].
            action_ids: The actions take.

        ---
        Returns:
            log_probs: The log-probabilities of the sampled actions.
                Shape of [batch_size,].
            entropies: The entropy of the categorical distributions.
                The entropies are normalized by the log of the number of actions.
                Shape of [batch_size,].
        """
        categorical = Categorical(probs=probs)
        n_actions = probs.shape[-1]

        entropies = categorical.entropy() / np.log(n_actions)
        log_probs = categorical.log_prob(action_ids)
        return log_probs, entropies

    @staticmethod
    def selective_add(
        tiles: torch.Tensor, embeddings: torch.Tensor, tile_ids: torch.Tensor
    ) -> torch.Tensor:
        """Add the given embeddings only to the specified tiles.

        ---
        Args:
            tiles: The original tile embeddings.
                Shape of [n_tiles, batch_size, embedding_dim].
            embeddings: The embeddings to add.
                Shape of [batch_size, embedding_dim].
            tile_ids: The tile ids to add the embeddings to.
                Shape of [batch_size,].

        ---
        Returns:
            The new tile embeddings.
                Shape of [n_tiles, batch_size, embedding_dim].
        """
        n_tiles, batch_size, _ = tiles.shape
        device = tiles.device

        offsets = torch.arange(0, batch_size * n_tiles, n_tiles, device=device)
        tiles = rearrange(tiles, "t b e -> (b t) e")
        tiles[tile_ids + offsets] = tiles[tile_ids + offsets] + embeddings
        tiles = rearrange(tiles, "(b t) e -> t b e", b=batch_size)

        return tiles
