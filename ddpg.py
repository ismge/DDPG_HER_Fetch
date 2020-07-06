import copy
import numpy as np
import torch
import torch.optim as optim

from model import Actor, Critic
from collections import OrderedDict
from normalizer import Normalizer
from replay_buffer import ReplayBuffer


class ddpgAgent(object):
    def __init__(self, params):
        """Implementation of DDPG with Hindsight Experience Replay (HER).
        """
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.input_dims = params['dims']
        self.buffer_size = params['buffer_size']
        self.tau = params['tau']
        self.batch_size = params['batch_size']
        self.critic_lr = params['lr_critic']
        self.actor_lr = params['lr_actor']
        self.norm_eps = params['norm_eps']
        self.norm_clip = params['norm_clip']
        self.clip_obs = params['clip_obs']
        self.clip_action = params['clip_action']

        self.T = params['T']
        self.rollout_batch_size = params['num_workers']
        self.clip_return = params['clip_return']
        self.sample_transitions = params['sample_her_transitions']
        self.gamma = params['gamma']

        self.replay_strategy = params['replay_strategy']

        self.dimo = self.input_dims['o']
        self.dimg = self.input_dims['g']
        self.dimu = self.input_dims['u']

        stage_shapes = OrderedDict()
        for key in sorted(self.input_dims.keys()):
            if key.startswith('info_'):
                continue
            stage_shapes[key] = (None, self.input_dims[key])
        stage_shapes['o_2'] = stage_shapes['o']
        stage_shapes['r'] = (None,)
        self.stage_shapes = stage_shapes

        # normalizer
        self.obs_normalizer = Normalizer(size=self.dimo, eps=self.norm_eps, default_clip_range=self.norm_clip)
        self.goal_normalizer = Normalizer(size=self.dimg, eps=self.norm_eps, default_clip_range=self.norm_clip)

        # networks
        self.actor_local = Actor(self.input_dims).to(self.device)
        self.critic_local = Critic(self.input_dims).to(self.device)
        self.actor_target = copy.deepcopy(self.actor_local)
        self.critic_target = copy.deepcopy(self.critic_local)

        # optimizers
        self.actor_optimizer = optim.Adam(self.actor_local.parameters(), lr=self.actor_lr)
        self.critic_optimizer = optim.Adam(self.critic_local.parameters(), lr=self.critic_lr)

        # Configure the replay buffer.
        buffer_shapes = {key: (self.T-1 if key != 'o' else self.T, self.input_dims[key])
                         for key, val in self.input_dims.items()}
        buffer_shapes['g'] = (buffer_shapes['g'][0], self.dimg)
        buffer_shapes['ag'] = (self.T, self.dimg)
        buffer_size = (self.buffer_size // self.rollout_batch_size) * self.rollout_batch_size

        self.buffer = ReplayBuffer(buffer_shapes, buffer_size, self.T, self.sample_transitions)


    def act(self, o, g, noise_eps=0., random_eps=0.):

        obs = self.obs_normalizer.normalize(o)
        goals = self.goal_normalizer.normalize(g)

        obs = torch.tensor(obs).to(self.device)
        goals = torch.tensor(goals).to(self.device)

        actions = self.actor_local(torch.cat([obs, goals], dim=1))

        noise = (noise_eps * np.random.randn(actions.shape[0], 4)).astype(np.float32)
        actions += torch.tensor(noise).to(self.device)

        eps_greedy_noise = np.random.binomial(1, random_eps, actions.shape[0]).reshape(-1, 1)

        random_action = torch.tensor(np.random.uniform(
            low=-1., high=1., size=(actions.shape[0], self.dimu)).astype(np.float32)).to(self.device)

        actions += torch.tensor(eps_greedy_noise.astype(np.float32)).to(self.device) * (
                    random_action - actions)  # eps-greedy

        actions = torch.clamp(actions, -self.clip_action, self.clip_action)
        return actions

    def store_episode(self, episode_batch):
        """
        episode_batch: array of batch_size x (T or T+1) x dim_key
                       'o' is of size T+1, others are of size T
        """
        self.buffer.store_episode(episode_batch)

        # add transitions to normalizer
        episode_batch['o_2'] = episode_batch['o'][:, 1:, :]
        episode_batch['ag_2'] = episode_batch['ag'][:, 1:, :]
        shape = episode_batch['u'].shape
        num_normalizing_transitions = shape[0] * shape[1]  # num_rollouts * (rollout_horizon - 1) --> total steps per cycle
        transitions = self.sample_transitions(episode_batch, num_normalizing_transitions)

        self.obs_normalizer.update(transitions['o'])
        self.obs_normalizer.recompute_stats()

        self.goal_normalizer.update(transitions['g'])
        self.goal_normalizer.recompute_stats()

    def sample_batch(self):
        transitions = self.buffer.sample(self.batch_size)
        return [transitions[key] for key in self.stage_shapes.keys()]

    def train(self):
        # TODO: delete unneeded steps
        batch = self.sample_batch()
        batch_dict = OrderedDict([(key, batch[i].astype(np.float32).copy())
                                 for i, key in enumerate(self.stage_shapes.keys())])
        batch_dict['r'] = np.reshape(batch_dict['r'], [-1, 1])

        local_net_batch = batch_dict
        target_net_batch = batch_dict.copy()
        target_net_batch['o'] = batch_dict['o_2']

        # LOCAL NETWORK ----------------------------------------------------
        obs = self.obs_normalizer.normalize(local_net_batch['o'])
        goal = self.goal_normalizer.normalize(local_net_batch['g'])
        obs = torch.tensor(obs).to(self.device)
        goal = torch.tensor(goal).to(self.device)
        actions = torch.tensor(local_net_batch['u']).to(self.device)

        policy_output = self.actor_local(torch.cat([obs, goal], dim=1))
        # temporary
        self.local_net_pi = policy_output  # action expected
        self.local_net_q_pi = self.critic_local(torch.cat([obs, goal], dim=1), policy_output)
        self.local_net_q = self.critic_local(torch.cat([obs, goal], dim=1), actions)

        # TARGET NETWORK ---------------------------------------------------
        obs = self.obs_normalizer.normalize(target_net_batch['o'])
        goal = self.goal_normalizer.normalize(target_net_batch['g'])
        obs = torch.tensor(obs).to(self.device)
        goal = torch.tensor(goal).to(self.device)
        actions = torch.tensor(target_net_batch['u']).to(self.device)

        policy_output = self.actor_target(torch.cat([obs, goal], dim=1))
        # temporary
        #self.target_net_pi = policy_output  # action expected
        self.target_net_q_pi = self.critic_target(torch.cat([obs, goal], dim=1), policy_output)
        #self.target_net_q = self.critic_target(torch.cat([obs, goal], dim=1), actions)

        # Q function loss
        rewards = torch.tensor(local_net_batch['r'].astype(np.float32)).to(self.device)
        discounted_reward = self.gamma * self.target_net_q_pi
        target_net = torch.clamp(rewards + discounted_reward, -self.clip_return, 0.)
        q_loss = torch.nn.MSELoss()(target_net.detach(), self.local_net_q)

        self.critic_optimizer.zero_grad()
        q_loss.backward()
        self.critic_optimizer.step()

        # policy loss
        pi_loss = -self.local_net_q_pi.mean()
        pi_loss += (self.local_net_pi ** 2).mean()

        self.actor_optimizer.zero_grad()
        pi_loss.backward()
        self.actor_optimizer.step()

    def update_target_net(self):
        # ----------------------- update target networks ----------------------- #
        self.soft_update(self.critic_local, self.critic_target, self.tau)
        self.soft_update(self.actor_local, self.actor_target, self.tau)

    def soft_update(self, local_model, target_model, tau):
        """Soft update model parameters.
        θ_target = τ*θ_local + (1 - τ)*θ_target

        Params
        ======
            local_model: PyTorch model (weights will be copied from)
            target_model: PyTorch model (weights will be copied to)
            tau (float): interpolation parameter
        """
        for target_param, local_param in zip(target_model.parameters(), local_model.parameters()):
            target_param.data.copy_(tau * local_param.data + (1.0 - tau) * target_param.data)

    def save_checkpoint(self, path, name):
        torch.save(self.actor_local.state_dict(), path + '/'+name+'_checkpoint_actor_her.pth')
        torch.save(self.critic_local.state_dict(), path + '/'+name+'_checkpoint_critic_her.pth')
