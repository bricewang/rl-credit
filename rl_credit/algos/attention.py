"""
A2C with an attention layer in the critic
"""
from abc import ABC, abstractmethod
import torch

from rl_credit.algos.base import BaseAlgo
from rl_credit.format import default_preprocess_obss
from rl_credit.utils import DictList, ParallelEnv
import rl_credit.script_utils as utils

import numpy as np
import torch.nn.functional as F

from rl_credit.model import ACAttention


class AttentionAlgo(BaseAlgo):
    """The Advantage Actor-Critic algorithm with attention used in the Critic."""

    def __init__(self, envs, acmodel, device=None, num_frames_per_proc=None, discount=0.99, lr=0.01, gae_lambda=0.95,
                 entropy_coef=0.01, value_loss_coef=0.5, max_grad_norm=0.5, recurrence=4,
                 rmsprop_alpha=0.99, rmsprop_eps=1e-8, preprocess_obss=None, reshape_reward=None,
                 wandb_dir=None):
        num_frames_per_proc = num_frames_per_proc or 8

        super().__init__(envs, acmodel, device, num_frames_per_proc, discount, lr, gae_lambda, entropy_coef,
                         value_loss_coef, max_grad_norm, recurrence, preprocess_obss, reshape_reward)

        self.optimizer = torch.optim.RMSprop(self.acmodel.parameters(), lr,
                                             alpha=rmsprop_alpha, eps=rmsprop_eps)

        self._update_number = 0  # convenience, for debugging, occasional saves
        self.wandb_dir = wandb_dir

    def collect_experiences(self):
        """Collects rollouts and computes advantages.

        Runs several environments concurrently. The next actions are computed
        in a batch mode for all environments at the same time. The rollouts
        and advantages from all environments are concatenated together.

        Returns
        -------
        exps : DictList
            Contains actions, rewards, advantages etc as attributes.
            Each attribute, e.g. `exps.reward` has a shape
            (self.num_frames_per_proc * num_envs, ...). k-th block
            of consecutive `self.num_frames_per_proc` frames contains
            data obtained from the k-th environment. Be careful not to mix
            data from different environments!
        logs : dict
            Useful stats about the training process, including the average
            reward, policy loss, value loss, etc.
        """

        for i in range(self.num_frames_per_proc):
            # Do one agent-environment interaction

            preprocessed_obs = self.preprocess_obss(self.obs, device=self.device)
            # preprocessed_obs shape: [num_procs, h, w, c]
            # add extra dim for ep len so shape -> [num_procs, 1, h, w, c]
            preprocessed_obs = torch.unsqueeze(preprocessed_obs, dim=1)

            with torch.no_grad():
                dist, _ = self.acmodel(preprocessed_obs)
            action = dist.sample()

            next_obs, reward, done, _ = self.env.step(action.cpu().numpy())

            # Update experiences values
            self.obss[i] = self.obs
            self.actions[i] = action
            self.masks[i] = self.mask
            self.mask = 1 - torch.tensor(done, device=self.device, dtype=torch.float)
            self.log_probs[i] = dist.log_prob(action)
            self.seq_label_delta = (1 - self.masks[i-1]) if i > 0 else 0
            self.seq_labels[i] = self.seq_labels[i-1] + self.seq_label_delta if i > 0 \
                                 else self.seq_labels[i]
            if self.reshape_reward is not None:
                self.rewards[i] = torch.tensor([
                    self.reshape_reward(obs_, action_, reward_, done_)
                    for obs_, action_, reward_, done_ in zip(next_obs, action, reward, done)
                ], device=self.device)
            else:
                self.rewards[i] = torch.tensor(reward, device=self.device)

            # update obs
            self.obs = next_obs

            # Update log values
            self.log_episode_return += torch.tensor(reward, device=self.device, dtype=torch.float)
            self.log_episode_reshaped_return += self.rewards[i]
            self.log_episode_num_frames += torch.ones(self.num_procs, device=self.device)

            for j, done_ in enumerate(done):
                if done_:
                    self.log_done_counter += 1
                    self.log_return.append(self.log_episode_return[j].item())
                    self.log_reshaped_return.append(self.log_episode_reshaped_return[j].item())
                    self.log_num_frames.append(self.log_episode_num_frames[j].item())

                    # log final reward
                    self.log_last_reward.append(self.rewards[i, j].item())

            self.log_episode_return *= self.mask
            self.log_episode_reshaped_return *= self.mask
            self.log_episode_num_frames *= self.mask

        # Reshape obss into tensor of size (batch size=num_procs, seq len=frames per proc, *(image_dim))
        obss_mat = [None]*(self.num_procs)
        for i in range(self.num_procs):
            obss_mat[i] = self.preprocess_obss([self.obss[j][i] for j in range(self.num_frames_per_proc)])
        obss_mat = torch.cat(obss_mat).view(self.num_procs, *obss_mat[0].shape)

        # Create additional obs mat including last obs for bootstrapping of size
        # (batch size=num_procs, seq len=frames per proc + 1, *(image_dim))
        next_obs = self.preprocess_obss(self.obs, device=self.device)
        next_obss_mat = torch.cat((obss_mat, next_obs.unsqueeze(1)), 1)

        # Define experiences:
        #   the whole experience is the concatenation of the experience
        #   of each process.
        # In comments below:
        #   - T is self.num_frames_per_proc,
        #   - P is self.num_procs,
        #   - D is the dimensionality.
        exps = DictList()
        # exps.obs = [self.obss[i][j]
        #             for j in range(self.num_procs)
        #             for i in range(self.num_frames_per_proc)]
        # T x P -> P x T -> (P * T) x 1
        exps.mask = self.masks.transpose(0, 1).reshape(-1).unsqueeze(1)
        # for all tensors below, T x P -> P x T -> P * T
        exps.action = self.actions.transpose(0, 1).reshape(-1)
        exps.reward = self.rewards.transpose(0, 1).reshape(-1)
        exps.log_prob = self.log_probs.transpose(0, 1).reshape(-1)

        # ===== Add advantage and return to experiences =====

        # Create block diagonal mask for observations from different
        # episodes don't pay attention to each other.
        # T+1 x P -> P x T+1 -> P x 1 x T+1 -> P x T+1 x T+1
        next_seq_label_delta = 1 - self.mask
        next_seq_label = self.seq_labels[-1] + next_seq_label_delta

        seq_labels = (torch.cat((self.seq_labels, next_seq_label.unsqueeze(0)), 0)
                      .transpose(0, 1)
                      .unsqueeze(1)
                      .expand(-1, self.num_frames_per_proc + 1, -1))
        # mask picks out elements outside the block diagonal to be masked out
        self.attn_mask = (seq_labels - seq_labels.transpose(2, 1)) != 0

        # Calculate values using whole context from episode
        with torch.no_grad():
            _, value, _ = self.acmodel(next_obss_mat, mask_future=True, attn_custom_mask=self.attn_mask)

        # for bootstrapping final value for unfinished trajectories cut off by end
        # of epoch (=num frames per proc)
        next_value = value.view(self.num_frames_per_proc + 1, self.num_procs)[-1]

        # drop value and masking corresponding to last obs
        self.values = value.view(self.num_frames_per_proc + 1, self.num_procs)[:-1, :]
        self.attn_mask = self.attn_mask[:, :-1, :-1]
        self.seq_labels_debug = seq_labels[:, :-1, :-1]  # only for debugging use

        # Bootstrap alternative 2: set last values to 0
        # next_value = torch.zeros(self.num_frames_per_proc, device=self.device)

        # Bootstrap alternative 3: approximate as value of the last obs
        # next_value = self.values[-1, :]

        for i in reversed(range(self.num_frames_per_proc)):
            next_mask = self.masks[i+1] if i < self.num_frames_per_proc - 1 else self.mask
            next_value = self.values[i+1] if i < self.num_frames_per_proc - 1 else next_value
            next_advantage = self.advantages[i+1] if i < self.num_frames_per_proc - 1 else 0

            delta = self.rewards[i] + self.discount * next_value * next_mask - self.values[i]
            self.advantages[i] = delta + self.discount * self.gae_lambda * next_advantage * next_mask

        exps.value = self.values.transpose(0, 1).reshape(-1)
        exps.advantage = self.advantages.transpose(0, 1).reshape(-1)
        exps.returnn = exps.value + exps.advantage
        # normalize the advantage
        exps.advantage = (exps.advantage - exps.advantage.mean())/exps.advantage.std()
        # Log some values

        keep = max(self.log_done_counter, self.num_procs)

        logs = {
            "return_per_episode": self.log_return[-keep:],
            "reshaped_return_per_episode": self.log_reshaped_return[-keep:],
            "num_frames_per_episode": self.log_num_frames[-keep:],
            "num_frames": self.num_frames,
            "last_reward_per_episode": self.log_last_reward[-keep:]
        }

        self.log_done_counter = 0
        self.log_return = self.log_return[-self.num_procs:]
        self.log_reshaped_return = self.log_reshaped_return[-self.num_procs:]
        self.log_num_frames = self.log_num_frames[-self.num_procs:]

        return obss_mat, exps, logs

    def update_parameters(self, obss, exps):
        self._update_number += 1
        logs = {}

        # ===== Calculate losses =====

        dist, value, scores = self.acmodel(obss, mask_future=True, attn_custom_mask=self.attn_mask)

        entropy = dist.entropy().mean()

        policy_loss = -(dist.log_prob(exps.action) * exps.advantage).mean()

        value_loss = (value - exps.returnn).pow(2).mean()

        loss = policy_loss - self.entropy_coef * entropy + self.value_loss_coef * value_loss

        # Update actor-critic

        self.optimizer.zero_grad()
        loss.backward()
        update_grad_norm = sum(p.grad.data.norm(2) ** 2 for p in self.acmodel.parameters()) ** 0.5
        torch.nn.utils.clip_grad_norm_(self.acmodel.parameters(), self.max_grad_norm)
        self.optimizer.step()

        # Log some values

        # Save attention scores heatmap every 100 updates
        if self.wandb_dir is not None and self._update_number % 100 == 0:
            import os
            import wandb
            import seaborn as sns
            import matplotlib.pyplot as plt
            attn_fig = (sns.heatmap(scores[0].detach().numpy(), xticklabels=10, yticklabels=10)
                        .get_figure())
            img_name_base = str(os.path.join(self.wandb_dir,
                                             f'attn_scores_{self._update_number:04}'))
            attn_fig.savefig(img_name_base, fmt='png')
            wandb.save(img_name_base + '*')
            plt.clf()

            # # For debugging
            # labels_fig = (sns.heatmap(self.seq_labels_debug[0].detach().numpy(), xticklabels=10, yticklabels=10)
            #               .get_figure())
            # labels_fig_base = str(os.path.join(self.wandb_dir,
            #                                    f'episode_labels_{self._update_number:04}'))
            # labels_fig.savefig(labels_fig_base, fmt='png')
            # plt.clf()

            mask_fig = (sns.heatmap(self.attn_mask[0].detach().numpy(), xticklabels=10, yticklabels=10)
                        .get_figure())
            mask_fig_base = str(os.path.join(self.wandb_dir,
                                             f'mask_{self._update_number:04}'))
            mask_fig.savefig(mask_fig_base, fmt='png')
            plt.clf()

        with torch.no_grad():
            # evaluate KL divergence b/w old and new policy
            # policy under newly updated model
            dist, _, _ = self.acmodel(obss)

            approx_kl = (exps.log_prob - dist.log_prob(exps.action)).mean().item()
            adv_mean = exps.advantage.mean().item()
            adv_max = exps.advantage.max().item()
            adv_min = exps.advantage.min().item()
            adv_std = exps.advantage.std().item()

            # standard deviation of values
            value_std = value.std().item()

        logs.update({
            "entropy": entropy.item(),
            "value": value.mean().item(),
            "value_std": value_std,
            "policy_loss": policy_loss.item(),
            "value_loss": value_loss.item(),
            "grad_norm": update_grad_norm.item(),
            "adv_max": adv_max,
            "adv_min": adv_min,
            "adv_mean": adv_mean,
            "adv_std": adv_std,
            "kl": approx_kl,
        })
        return logs

    def _get_starting_indexes(self):
        """Gives the indexes of the observations given to the model and the
        experiences used to compute the loss at first.

        The indexes are the integers from 0 to `self.num_frames` with a step of
        `self.recurrence`. If the model is not recurrent, they are all the
        integers from 0 to `self.num_frames`.

        Returns
        -------
        starting_indexes : list of int
            the indexes of the experiences to be used at first
        """

        starting_indexes = np.arange(0, self.num_frames, self.recurrence)
        return starting_indexes


def get_obss_preprocessor(obs_space):
    import gym
    # Check if it is a MiniGrid observation space
    if isinstance(obs_space, gym.spaces.Dict) and list(obs_space.spaces.keys()) == ["image"]:
        obs_space = obs_space.spaces["image"].shape

        def preprocess_obss(obss, device=None):
            images = np.array([obs["image"] for obs in obss])
            return torch.tensor(images, device=device, dtype=torch.float)
    else:
        raise ValueError("Unknown observation space: " + str(obs_space))

    return obs_space, preprocess_obss


if __name__ == '__main__':
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    num_procs = 4
    seed = 0

    envs = []
    for i in range(num_procs):
        envs.append(utils.make_env('MiniGrid-KeyGoal-6x6-v0', seed + 10000 * i))

    obs_space, preprocess_obss = get_obss_preprocessor(envs[0].observation_space)
    acmodel = ACAttention(obs_space, envs[0].action_space,)

    algo_args=dict(device=device,
                   num_frames_per_proc=128,
                   discount=1.,
                   lr=0.001,
                   gae_lambda=1.,
                   entropy_coef=0.,
                   value_loss_coef=0.5,
                   max_grad_norm=0.5,
                   recurrence=1,
                   rmsprop_alpha=0.99,
                   rmsprop_eps=1e-8,
                   preprocess_obss=preprocess_obss)
    algo = AttentionAlgo(envs, acmodel, **algo_args)

    total_frames = 0
    obss, exps, logs = algo.collect_experiences()
    algo.update_parameters(obss, exps)
    total_frames += logs['num_frames']
