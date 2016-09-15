# -*- coding: utf-8 -*-
"""
Teaching a machine to play an Atari game (Pacman by default) by implementing
a 1-step Q-learning with TFLearn, TensorFlow and OpenAI gym environment. The
algorithm is described in "Asynchronous Methods for Deep Reinforcement Learning"
paper. OpenAI's gym environment is used here for providing the Atari game
environment for handling games logic and states. This example is originally
adapted from Corey Lynch's repo (url below).

Requirements:
    - gym environment (pip install gym)
    - gym Atari environment (pip install gym[atari])

References:
    - Asynchronous Methods for Deep Reinforcement Learning. Mnih et al, 2015.

Links:
    - Paper: http://arxiv.org/pdf/1602.01783v1.pdf
    - OpenAI's gym: https://gym.openai.com/
    - Original Repo: https://github.com/coreylynch/async-rl

"""
from __future__ import division, print_function, absolute_import

import threading
import random
import numpy as np
import time
from skimage.transform import resize
from skimage.color import rgb2gray
from collections import deque

import gym
import mxnet as mx

# Change that value to test instead of train
testing = False
# Model path (to load when testing)
# Atari game to learn
# You can also try: 'Breakout-v0', 'Pong-v0', 'SpaceInvaders-v0', ...
game = 'Breakout-v0'
# Learning threads
n_threads = 8
f_log = open(game + '.txt', 'w')
# =============================
#   Training Parameters
# =============================
# Max training steps
TMAX = 80000000
# Current training step
T = 0
# Consecutive screen frames when performing training
action_repeat = 4
# Async gradient update frequency of each learning thread
I_AsyncUpdate = 5
# Timestep to reset the target network
I_target = 40000
# Learning rate
learning_rate = 0.001
# Reward discount rate
gamma = 0.99
# Number of timesteps to anneal epsilon
anneal_epsilon_timesteps = 400000

# =============================
#   Utils Parameters
# =============================
# Display or not gym evironment screens
show_training = False
# Directory for storing tensorboard summaries
checkpoint_interval = 200000
# Number of episodes to run gym evaluation
num_eval_episodes = 100
ctx = mx.gpu(2)
batch_size = 32
input_shape = (32, 4, 84, 84)
input_scale = 255.0


# =============================
#   TFLearn Deep Q Network
# =============================
class DQNInitializer(mx.initializer.Uniform):
    def __init__(self):
        mx.initializer.Uniform.__init__(self)

    def _init_bias(self, _, arr):
        arr[:] = .1

    def _init_default(self, name, _):
        pass


def copy_weights(from_exe, to_exe):
    for k in from_exe.arg_dict:
        if k.endswith('weight') or k.endswith('bias'):
            from_exe.arg_dict[k].copyto(to_exe.arg_dict[k])


def share_weights(source_exe, to_exe):
    for k in source_exe.arg_dict:
        if k.endswith('weight') or k.endswith('bias'):
            to_exe.arg_dict[k] = source_exe.arg_dict[k]


def init_exe(executor, initializer):
    for k, v in executor.arg_dict.items():
        initializer(k, v)


def update_weights(executor, updater):
    for ind, k in enumerate(executor.arg_dict):
        if k.endswith('weight') or k.endswith('bias'):
            updater(index=ind, grad=executor.grad_dict[k], weight=executor.arg_dict[k])


def build_nature_network(num_actions=20):
    data = mx.sym.Variable("data")
    conv1 = mx.sym.Convolution(data=data, num_filter=32, stride=(4, 4),
                               kernel=(8, 8), name="conv1")
    relu1 = mx.sym.Activation(data=conv1, act_type='relu', name="relu1")
    conv2 = mx.sym.Convolution(data=relu1, num_filter=64, stride=(2, 2),
                               kernel=(4, 4), name="conv2")
    relu2 = mx.sym.Activation(data=conv2, act_type='relu', name="relu2")
    conv3 = mx.sym.Convolution(data=relu2, num_filter=64, stride=(1, 1),
                               kernel=(3, 3), name="conv3")
    relu3 = mx.sym.Activation(data=conv3, act_type='relu', name="relu3")
    fc4 = mx.sym.FullyConnected(data=relu3, name="fc4", num_hidden=512)
    relu4 = mx.sym.Activation(data=fc4, act_type='relu', name="relu4")
    fc5 = mx.sym.FullyConnected(data=relu4, name="fc5", num_hidden=num_actions)
    return fc5


def build_graphs(input_shape, ctx, num_actions=20):
    batch_size, num_frames, input_width, input_height = input_shape
    q_values = build_nature_network(num_actions)
    target_q_values = mx.sym.Variable("target")
    action_mask = mx.sym.Variable("action")
    out_q_values = mx.sym.sum(q_values * action_mask, axis=1)
    loss = mx.sym.LinearRegressionOutput(data=out_q_values, label=target_q_values)
    loss_exe = loss.simple_bind(ctx=ctx, data=input_shape, grad_req='write')
    policy_exe = q_values.simple_bind(ctx=ctx, data=(1, num_frames, input_width, input_height), grad_req='null')
    target_exe = q_values.simple_bind(ctx=ctx, data=input_shape, grad_req='null')
    return loss_exe, policy_exe, target_exe

# =============================
#   ATARI Environment Wrapper
# =============================
class AtariEnvironment(object):
    """
    Small wrapper for gym atari environments.
    Responsible for preprocessing screens and holding on to a screen buffer
    of size action_repeat from which environment state is constructed.
    """
    def __init__(self, gym_env, action_repeat):
        self.env = gym_env
        self.action_repeat = action_repeat
	self.start_lives = self.env.ale.lives()
        # Agent available actions, such as LEFT, RIGHT, NOOP, etc...
        self.gym_actions = range(gym_env.action_space.n)
        # Screen buffer of size action_repeat to be able to build
        # state arrays of size [1, action_repeat, 84, 84]
        self.state_buffer = deque()

    def get_initial_state(self):
        """
        Resets the atari game, clears the state buffer.
        """
        # Clear the state buffer
        self.state_buffer = deque()

        x_t = self.env.reset()
        x_t = self.get_preprocessed_frame(x_t)
        s_t = np.stack([x_t for i in range(self.action_repeat)], axis=0)

        for i in range(self.action_repeat-1):
            self.state_buffer.append(x_t)
        return s_t

    def get_preprocessed_frame(self, observation):
        """
        0) Atari frames: 210 x 160
        1) Get image grayscale
        2) Rescale image 110 x 84
        3) Crop center 84 x 84 (you can crop top/bottom according to the game)
        """
        return resize(rgb2gray(observation), (84, 84))

    def step(self, action_index):
        """
        Excecutes an action in the gym environment.
        Builds current state (concatenation of action_repeat-1 previous
        frames and current one). Pops oldest frame, adds current frame to
        the state buffer. Returns current state.
        """

        x_t1, r_t, terminal, info = self.env.step(self.gym_actions[action_index])
        x_t1 = self.get_preprocessed_frame(x_t1)

        previous_frames = np.array(self.state_buffer)
        s_t1 = np.empty((self.action_repeat, 84, 84))
        s_t1[:self.action_repeat-1, :] = previous_frames
        s_t1[self.action_repeat-1] = x_t1

        # Pop the oldest frame, add the current frame to the queue
        self.state_buffer.popleft()
        self.state_buffer.append(x_t1)
	terminal = terminal or (self.env.ale.lives() < self.start_lives)
        return s_t1, r_t, terminal, info


# =============================
#   1-step Q-Learning
# =============================
def sample_final_epsilon():
    """
    Sample a final epsilon value to anneal towards from a distribution.
    These values are specified in section 5.1 of http://arxiv.org/pdf/1602.01783v1.pdf
    """
    final_epsilons = np.array([.1, .01, .5])
    probabilities = np.array([0.4, 0.3, 0.3])
    return np.random.choice(final_epsilons, 1, p=list(probabilities))[0]


def actor_learner_thread(thread_id, env, executors, num_actions, updater):
    """
    Actor-learner thread implementing asynchronous one-step Q-learning, as specified
    in algorithm 1 here: http://arxiv.org/pdf/1602.01783v1.pdf.
    """
    global TMAX, T
    # Wrap env with AtariEnvironment helper class
    env = AtariEnvironment(gym_env=env,
                           action_repeat=action_repeat)

    # Initialize network gradients
    s_batch = []
    a_batch = []
    r_batch = []
    t_batch = []
    next_batch = []

    loss_exe, policy_exe, target_exe = executors
    final_epsilon = sample_final_epsilon()
    initial_epsilon = 1.0
    epsilon = 1.0

    print("Thread " + str(thread_id) + " - Final epsilon: " + str(final_epsilon))

    time.sleep(3*thread_id)
    t = 0
    while T < TMAX:
        # Get initial game observation
        s_t = env.get_initial_state()
        terminal = False

        # Set up per-episode counters
        ep_reward = 0
        episode_ave_max_q = 0
        ep_t = 0
        ep_total_loss = 0

        while True:
            # Forward the deep q network, get Q(s,a) values
            st = mx.nd.array([s_t], ctx=ctx) / input_scale
            readout_t = policy_exe.forward(data=st)[0].asnumpy()

            # Choose next action based on e-greedy policy
            if random.random() <= epsilon:
                action_index = random.randrange(num_actions)
            else:
                action_index = np.argmax(readout_t)

            # Scale down epsilon
            if epsilon > final_epsilon:
                epsilon -= (initial_epsilon - final_epsilon) / anneal_epsilon_timesteps

            # Gym excecutes action in game environment on behalf of actor-learner
            s_t1, r_t, terminal, info = env.step(action_index)
            clipped_r_t = np.clip(r_t, -1, 1)

            s_batch.append(s_t)
            a_batch.append(action_index)
            r_batch.append(clipped_r_t)
            t_batch.append(terminal)
            next_batch.append(s_t1)

            # Update the state and counters
            s_t = s_t1
            T += 1
            t += 1

            ep_t += 1
            ep_reward += r_t
            episode_ave_max_q += np.max(readout_t)

            # Optionally update target network
            if T % I_target == 0:
                copy_weights(loss_exe, target_exe)

            # Optionally update online network
            if len(s_batch) == 32:
                    states = mx.nd.array(s_batch, ctx=ctx) / input_scale
                    actions = mx.nd.array(a_batch, ctx=ctx)
                    at_encoded = mx.nd.zeros((batch_size, num_actions), ctx=ctx)
                    mx.nd.onehot_encode(actions, at_encoded)
                    rewards = mx.nd.array(r_batch, ctx=ctx)
                    terminals = mx.nd.array(t_batch, ctx=ctx)
                    next_states = mx.nd.array(next_batch, ctx=ctx) / input_scale

                    next_q_values = target_exe.forward(data=next_states)[0]
                    target_q_values = rewards + mx.nd.choose_element_0index(next_q_values, mx.nd.argmax_channel(next_q_values)) * (1.0 - terminals) * gamma
                    out_q_values = loss_exe.forward(is_train=True, data=states, target=target_q_values, action=at_encoded)[0]
                    loss_exe.backward()
                    update_weights(loss_exe, updater)
                    copy_weights(loss_exe, policy_exe)

                    loss = mx.nd.square(out_q_values - target_q_values)
                    loss = mx.nd.sum(loss)*batch_size
                    ep_total_loss += loss.asnumpy()
                    # Clear gradients
                    s_batch = []
                    a_batch = []
                    t_batch = []
                    r_batch = []
                    next_batch = []

            # Save model progress
            if t % checkpoint_interval == 0:
                filename = game + ".params"
                mx.nd.save(filename, policy_exe.arg_dict)

            # Print end of episode stats
            if terminal:
                info_str = "Enduro: Thread %2d | Step %8d/%8d | Reward %3d | Qmax %.4f | Loss %.3f | Epsilon %.4f" % (thread_id, t, T, ep_reward, episode_ave_max_q/ep_t, ep_total_loss/ep_t, epsilon)
		f_log.write(info_str + '\n')
                print(info_str)
                break

def get_num_actions():
    """
    Returns the number of possible actions for the given atari game
    """
    # Figure out number of actions from gym env
    env = gym.make(game)
    num_actions = env.action_space.n
    return num_actions


def train(executors, num_actions):
    """
    Train a model.
    """

    # Set up game environments (one per thread)
    envs = [gym.make(game) for _ in range(n_threads)]
    loss_exe, policy_exe, target_exe = executors
    optimizer = mx.optimizer.create(name='RMSProp', learning_rate=0.001, gamma2=0.0, clip_gradient=1.0)
    updater = mx.optimizer.get_updater(optimizer)
    initializer = DQNInitializer()
    init_exe(loss_exe, initializer)
    copy_weights(loss_exe, target_exe)
    copy_weights(loss_exe, policy_exe)

    # Start n_threads actor-learner training threads
    actor_learner_threads = \
        [threading.Thread(target=actor_learner_thread,
                          args=(thread_id, envs[thread_id], executors, num_actions, updater))
         for thread_id in range(n_threads)]
    for t in actor_learner_threads:
        t.start()
        time.sleep(0.01)

    # Show the agents training and write summary statistics
    while True:
        if show_training:
            for env in envs:
                env.render()
    for t in actor_learner_threads:
        t.join()


def main():
    num_actions = get_num_actions()
    executors = build_graphs(input_shape, ctx, num_actions)
    train(executors, num_actions)

if __name__ == "__main__":
    main()

