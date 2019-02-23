#!/usr/bin/env python3

"""
Simple example of using cherry to solve cartpole.
The code is an adaptation of the PyTorch reinforcement learning example.
"""

import random
import gym
import numpy as np

from itertools import count

import pybullet_envs

import torch as th
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import normal
import copy

import inspect

import cherry as ch
import cherry.envs as envs
from cherry.rewards import discount_rewards
from cherry.utils import normalize
import cherry.policies as policies

SEED = 42
GAMMA = 0.99
RENDER = False

random.seed(SEED)
np.random.seed(SEED)
th.manual_seed(SEED)


class PolicyNet(nn.Module):
    def __init__(self, env):
        super(PolicyNet, self).__init__()
        self.affine1 = nn.Linear(env.state_size, 128)
        self.affine2 = nn.Linear(128, env.action_size)
        self.dist = policies.ActionDistribution(env)

    def forward(self, x):
        x = F.relu(self.affine1(x))
        action_scores = self.affine2(x)
        return self.dist(action_scores), 0



class Mlp(nn.Module):
    
    def identity(x):
        return x

    def __init__(
            self,
            hidden_sizes,
            output_size,
            input_size,
            init_w=3e-3,
            hidden_activation=F.relu,
            output_activation=identity,
            b_init_value=0.1,
    ):

        super(Mlp, self).__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.hidden_activation = hidden_activation
        self.output_activation = output_activation
        hidden_init = self.fanin_init,
        self.fcs = []
        in_size = input_size

        for i, next_size in enumerate(hidden_sizes):
            fc = nn.Linear(in_size, next_size)
            in_size = next_size
            fc.bias.data.fill_(b_init_value)
            self.fcs.append(fc)

        self.last_fc = nn.Linear(in_size, output_size)
        self.last_fc.weight.data.uniform_(-init_w, init_w)
        self.last_fc.bias.data.uniform_(-init_w, init_w)


    def forward(self, input):
        h = input
        for i, fc in enumerate(self.fcs):
            h = fc(h)
            h = self.hidden_activation(h)
        preactivation = self.last_fc(h)
        output = self.output_activation(preactivation)
        return output

    def fanin_init(self, tensor):
        size = tensor.size()
        if len(size) == 2:
            fan_in = size[0]
        elif len(size) > 2:
            fan_in = np.prod(size[1:])
        else:
            raise Exception("Shape must be have dimension at least 2.")
        bound = 1. / np.sqrt(fan_in)
        return tensor.data.uniform_(-bound, bound)


class FlattenMlp(Mlp):
    """
    Flatten inputs along dimension 1 and then pass through MLP.
    """
    def forward(self, *args, **kwargs):
        flat_inputs = th.cat(args, dim=1)
        return super().forward(flat_inputs, **kwargs)

class TanhGaussianPolicy(Mlp):
    def __init__(
            self,
            hidden_sizes,
            obs_dim,
            action_dim,
            epsilon=1e-6,
            std=None,
            init_w=1e-3,
            **kwargs
    ):
        super().__init__(
            hidden_sizes,
            input_size=obs_dim,
            output_size=action_dim,
            init_w=init_w,
            **kwargs
        )
        self.log_std = None
        self.std = std
        self.epsilon = epsilon
        if std is None:
            last_hidden_size = obs_dim
            if len(hidden_sizes) > 0:
                last_hidden_size = hidden_sizes[-1]
            self.last_fc_log_std = nn.Linear(last_hidden_size, action_dim)
            self.last_fc_log_std.weight.data.uniform_(-init_w, init_w)
            self.last_fc_log_std.bias.data.uniform_(-init_w, init_w)
        else:
            self.log_std = np.log(std)
            assert -20 <= self.log_std <= 2 

    def forward(
            self,
            obs,
            reparameterize=True,
            deterministic=False,
            return_log_prob=False,
    ):
        h = obs
        for i, fc in enumerate(self.fcs):
            h = self.hidden_activation(fc(h))
        mean = self.last_fc(h)
        if self.std is None:
            log_std = self.last_fc_log_std(h)
            log_std = th.clamp(log_std, -20, 2) 
            std = th.exp(log_std)
        else:
            std = self.std
            log_std = self.log_std

        normal_dist = normal.Normal(mean, std)
        log_prob = None
        entropy = None
        mean_action_log_prob = None
        pre_tanh_value = None
        if deterministic:
            action = th.tanh(mean)
        else:
            z = normal_dist.sample().detach()
            action = th.tanh(z)

            pre_tanh_value = th.log((1+action)/(1-action))/2
            z = normal_dist.log_prob(pre_tanh_value) - th.log(1-action * action + self.epsilon)
            log_prob = th.tanh(z)

        return (
            action, mean, log_prob
        )

class SoftActorCritic():
    def __init__(
            self,
            env,
            policy,
            qf,
            vf,
            policy_optimizer,
            qf_optimizer,
            vf_optimizer,
            target_vf,

            policy_lr=1e-3,
            qf_lr=1e-3,
            vf_lr=1e-3,
            policy_mean_reg_weight=1e-3,
            policy_std_reg_weight=1e-3,
            policy_pre_activation_weight=0.,
            discount=.99,

            train_policy_with_reparameterization=True,
            soft_target_tau=1e-2,
            plotter=None,
            render_eval_paths=False,

            use_automatic_entropy_tuning=True,
            target_entropy=None,
            **kwargs
    ):

        self.policy = policy
        self.qf = qf
        self.vf = vf
        self.env = env
        self.train_policy_with_reparameterization = (
            train_policy_with_reparameterization
        )
        self.soft_target_tau = soft_target_tau
        self.policy_mean_reg_weight = policy_mean_reg_weight
        self.policy_std_reg_weight = policy_std_reg_weight
        self.policy_pre_activation_weight = policy_pre_activation_weight
        self.plotter = plotter
        self.render_eval_paths = render_eval_paths
        self.use_automatic_entropy_tuning = use_automatic_entropy_tuning
        if self.use_automatic_entropy_tuning:
            if target_entropy:
                self.target_entropy = target_entropy
            else:
                self.target_entropy = -np.prod(self.env.action_space.shape).item()  # heuristic value from Tuomas
            self.log_alpha = th.zeros(1, requires_grad=True)
            self.alpha_optimizer = optim.Adam([self.log_alpha], lr=policy_lr)

        self.target_vf = target_vf 
        self.qf_criterion = nn.MSELoss()
        self.vf_criterion = nn.MSELoss()

        self.policy_optimizer = policy_optimizer
        self.qf_optimizer = qf_optimizer
        self.vf_optimizer = vf_optimizer
        
        self.discount = discount

    def update(self, replay, env):

        batch = replay.sample(40)
        actions, values, log_pi = self.policy(batch.states)
        log_pi = log_pi.sum(-1).detach()
        log_pi = log_pi.view(-1,1)
        
        print("Average Rewards: ") 
        print(batch.rewards.mean())
        print("\n")


        ''' Calculate Alpha Loss '''
        if self.use_automatic_entropy_tuning:
            alpha_loss = -(self.log_alpha * (log_pi + self.target_entropy).detach()).mean()
            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()
            alpha = self.log_alpha.exp()
        else:
            alpha = 1
            alpha_loss = 0

        '''
        QF Loss

        Minimize Soft Bellman Residual, i.e.
        J_{Q}(theta) = E_{(s_t, a_t)~D}[grad[Q(a,s)](Q(s,a)-(r(s,a)+gamma(Q'(s', a')-alpha*log(pi(a'|s')))))
        and grad of J_{Q} is:
        grad(Q(t))*(Q(t) - (r(t) + gamma*(Q'(t+1) - alpha*log_pi(t+1))))

        '''
        q_pred = self.qf(batch.states, batch.actions)
        target_v_values = self.target_vf(batch.next_states)
        q_target = batch.rewards + (1. - batch.dones) * self.discount * target_v_values
        qf_loss = self.qf_criterion(q_pred, q_target)
        print("QF Loss: ") 
        print(qf_loss)
        print("\n")


        #   For some reason the loop is not running (/iterating of batch), causing qf_loss to
        #   always be zero 


        """
        VF Loss
        
        Below is code to calculate the VF Loss. Making it work is not a priority because of the note on
        page 6 of Haarnoja's paper from December 2018, indicating that the value function approximator is
        unnecessary.
        """
        
        q_new_actions = self.qf(batch.next_states, batch.actions)
        v_pred = self.vf(batch.states)
        v_target = q_new_actions - alpha*log_pi
        vf_loss = self.vf_criterion(v_pred, v_target.detach())
        print("VF Loss: ") 
        print(vf_loss)
        print("\n")
        

        ''' Calculate Policy Loss '''

        policy_loss = alpha*log_pi - q_new_actions
        policy_loss = policy_loss.mean()
        print("Policy Loss: ") 
        print(policy_loss)
        print("\n\n")
        #policy_loss /= batch.__len__()

        # TODO: calculate regression loss and add to policy_loss


        ''' Update Networks '''

        self.qf_optimizer.zero_grad()
        qf_loss.backward()
        self.qf_optimizer.step()

        self.vf_optimizer.zero_grad()
        vf_loss.backward()
        self.vf_optimizer.step()

        self.policy_optimizer.zero_grad()
        policy_loss.backward()
        self.policy_optimizer.step()

        for target_param, param in zip(self.target_vf.parameters(), self.vf.parameters()):
           target_param.data.copy_(
               target_param.data * (1.0 - self.soft_target_tau) + param.data * self.soft_target_tau
           )


    def np_to_pytorch_batch(self, np_batch):
        return {
            k: _elem_or_tuple_to_variable(x)
            for k, x in self._filter_batch(np_batch)
            if x.dtype != np.dtype('O')  # ignore object (e.g. dictionaries)
        }

    def _filter_batch(self, np_batch):
        for k, v in np_batch.items():
            if v.dtype == np.bool:
                yield k, v.astype(int)
            else:
                yield k, v

    def _elem_or_tuple_to_variable(self, elem_or_tuple):
        if isinstance(elem_or_tuple, tuple):
            return tuple(
                self._elem_or_tuple_to_variable(e) for e in elem_or_tuple
            )
        return self.from_numpy(elem_or_tuple).float()

    def from_numpy(*args, **kwargs):
        return th.from_numpy(*args, **kwargs).float().to(device)

#
# End SoftActorCritic Class
#

def get_action_value(state, policy):
    action, value, log_pi = policy(state)
    return action, value, log_pi

if __name__ == '__main__':
    #env = NormalizedBoxEnv(gym.make('AntBulletEnv-v0'))
    env = gym.make('AntBulletEnv-v0')
    env = envs.Logger(env, interval=1000)
    env = envs.Torch(env)
    env = envs.Runner(env)
    env.seed(SEED)

    obs_dim = int(np.prod(env.observation_space.shape))
    action_dim = int(np.prod(env.action_space.shape))
    net_size = 300

    #policy = PolicyNet(env)
    policy = TanhGaussianPolicy(hidden_sizes=[net_size, net_size], obs_dim=obs_dim, action_dim=action_dim)
    qnet = FlattenMlp(hidden_sizes=[net_size, net_size], input_size=obs_dim+action_dim, output_size=1)
    vnet = FlattenMlp(hidden_sizes=[net_size, net_size], input_size=obs_dim, output_size=1)
    target_vnet = copy.deepcopy(vnet)

    policy_optimizer = optim.Adam(policy.parameters(), lr=1e-2)
    qnet_optimizer = optim.Adam(qnet.parameters(), lr=1e-2)
    vnet_optimizer = optim.Adam(vnet.parameters(), lr=1e-2)

    running_reward = 10.0
    replay = ch.ExperienceReplay()
    
    critic = SoftActorCritic(env=env, policy=policy, qf=qnet, vf=vnet, policy_optimizer=policy_optimizer,
            qf_optimizer=qnet_optimizer, vf_optimizer=vnet_optimizer, target_vf=target_vnet)

    get_action = lambda state: get_action_value(state, policy)
    num_updates = 20000
    SAC_STEPS = 1000
    
    for epoch in range(num_updates):
        # We use the Runner collector, but could've written our own
        if RENDER:
            env.render()
        num_samples, num_episodes = env.run(get_action,
                                            replay,
                                            steps=SAC_STEPS,
                                            render=RENDER)

        # Update policy
        #update(replay, optimizer, policy, env, lr_schedule)
        critic.update(replay, env)

#####################################################################################################
#   SAC To Dos
#####################################################################################################
# Use to target Q functions to eliminate positive bias (SACAA 6)
'''
    -Strip everything out from Soft Actor Critic Class
    -Debug update function
    -Check output against rlkit
'''

