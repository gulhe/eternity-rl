from typing import Optional

import numpy as np
import torch
import torch.nn as nn
from torch.distributions import Categorical
from torchinfo import summary

from .backbone import Backbone
from .decoder import Decoder

N_SIDES, N_ACTIONS = 4, 4


class Policy(nn.Module):
    def __init__(
        self,
        n_classes: int,
        board_width: int,
        board_height: int,
        embedding_dim: int,
        backbone_layers: int,
        decoder_layers: int,
        dropout: float,
    ):
        super().__init__()
        self.board_width = board_width
        self.board_height = board_height

        self.backbone = Backbone(
            n_classes,
            embedding_dim,
            backbone_layers,
            dropout,
        )
        self.decoder = Decoder(
            embedding_dim,
            decoder_layers,
            dropout,
            n_queries=N_ACTIONS,
        )
        self.rnn = nn.GRUCell(embedding_dim, embedding_dim)
        self.norm = nn.LayerNorm(embedding_dim)

        predict_tile_ids = nn.Linear(embedding_dim, board_width * board_height)
        predict_roll_ids = nn.Linear(embedding_dim, N_SIDES)
        embed_tile_ids = nn.Sequential(
            nn.Embedding(board_width * board_height, embedding_dim),
            nn.LayerNorm(embedding_dim),
        )
        embed_roll_ids = nn.Sequential(
            nn.Embedding(4, embedding_dim),
            nn.LayerNorm(embedding_dim),
        )
        self.predict_actions = nn.ModuleList(
            [
                predict_tile_ids,
                predict_tile_ids,
                predict_roll_ids,
                predict_roll_ids,
            ]
        )
        self.embed_actions = nn.ModuleList(
            [
                embed_tile_ids,
                embed_tile_ids,
                embed_roll_ids,
                embed_roll_ids,
            ]
        )

        assert len(self.predict_actions) == len(self.embed_actions) == N_ACTIONS

    def dummy_input(self, device: str) -> tuple[torch.Tensor, torch.Tensor]:
        tiles = torch.zeros(
            1,
            N_SIDES,
            self.board_height,
            self.board_width,
            dtype=torch.long,
            device=device,
        )
        timesteps = torch.zeros(
            1,
            dtype=torch.long,
            device=device,
        )
        return tiles, timesteps

    def summary(self, device: str):
        """Torchinfo summary."""
        dummy_input = self.dummy_input(device)
        summary(
            self,
            input_data=[*dummy_input],
            depth=2,
            device=device,
        )

    def forward(
        self,
        tiles: torch.Tensor,
        timesteps: torch.Tensor,
        # game_hidden_state: Optional[torch.Tensor] = None,
        sampling_mode: str = "sample",
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Predict the actions and value for the given game states.

        ---
        Args:
            tiles: The game state.
                Tensor of shape [batch_size, 4, board_height, board_width].
            timestep: The timestep of the game states.
                Tensor of shape [batch_size,].
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
        tiles = self.backbone(tiles, timesteps)
        queries = self.decoder(tiles)

        actions, logprobs, entropies = [], [], []
        hidden_state = torch.zeros_like(queries[0], device=tiles.device)
        for query, predict_action, embed_action in zip(
            queries, self.predict_actions, self.embed_actions
        ):
            residual = hidden_state

            hidden_state = self.rnn(query, hidden_state)
            action_scores = predict_action(hidden_state)
            action_ids = self.sample_actions(action_scores, sampling_mode)

            action_embeddings = embed_action(action_ids)
            hidden_state = self.rnn(action_embeddings, hidden_state)

            hidden_state = hidden_state + residual
            hidden_state = self.norm(hidden_state)

            actions.append(action_ids)
            probs = torch.softmax(action_scores, dim=-1)
            actions_logprob, actions_entropy = Policy.logprobs(probs, action_ids)
            logprobs.append(actions_logprob)
            entropies.append(actions_entropy)

        actions = torch.stack(actions, dim=1)
        logprobs = torch.stack(logprobs, dim=1)
        entropies = torch.stack(entropies, dim=1)

        return actions, logprobs, entropies

    @staticmethod
    def sample_actions(logits: torch.Tensor, mode: str) -> torch.Tensor:
        match mode:
            case "sample":
                distributions = torch.softmax(logits, dim=-1)
                categorical = Categorical(probs=distributions)
                action_ids = categorical.sample()
            case "argmax":
                action_ids = torch.argmax(logits, dim=-1)
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
