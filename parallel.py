import os
import time
import argparse
from collections import deque
import numpy as np
import torch
import pybullet_envs.bullet.kuka_diverse_object_gym_env as e

import ray
ray.init()
time.sleep(1)

from base.memory import BaseMemory


@ray.remote(num_cpus=1)
class GymEnvironment:
    """Wrapper to run an environment on a remote CPU."""

    def __init__(self, model_creator, env_creator, seed=None):

        if seed is None:
            seed = np.random.randint(1234567890)

        np.random.seed(seed)
        torch.manual_seed(seed)

        self.model = model_creator()

        for p in self.model.model.parameters():
            p.requires_grad_(False)

        self.env = env_creator()
        self.reset()

    def step(self, action):
        return self.env.step(action)

    def reset(self):
        return self.env.reset()

    def rollout(self, weights, num_episodes=5, explore_prob=0.):
        """Performs a full grasping episode in the environment."""

        self.model.set_weights(weights)

        steps, rewards = [], []
        for _ in range(num_episodes):

            step, done = 0, False
            state = self.reset()

            while not done:

                state = state.transpose(2, 0, 1)[np.newaxis]
                state = state.astype(np.float32) / 255.

                action = self.model.sample_action(state,
                                                  float(step),
                                                  explore_prob)

                if not isinstance(action, np.ndarray):
                    action = action.cpu().numpy().flatten()

                state, reward, done, _ = self.step(action)

                step = step + 1

            steps.append(step - 1)
            rewards.append(reward)

        return (steps, rewards)


def make_env(max_steps, is_test, render):
    """Makes a new environment given a config file."""

    # Defines parameters for distributed evaluation
    ENV_CONFIG = {'actionRepeat':80,
                  'isEnableSelfCollision':True,
                  'renders':render,
                  'isDiscrete':False,
                  'maxSteps':max_steps,
                  'dv':0.06,
                  'removeHeightHack':True,
                  'blockRandom':0.3,
                  'cameraRandom':0,
                  'width':64,
                  'height':64,
                  'numObjects':5,
                  'isTest':is_test}

    def create():
        return e.KukaDiverseObjectEnv(**ENV_CONFIG)
    return create


def make_model(args, device):
    """Makes a new model given a config file."""

    # Defines parameters for network generator
    config = {'action_size':4, 'bounds':(-1, 1), 'device':device}
    config.update(vars(args))

    if args.model == 'dqn':
        from dqn import DQN as Model
    elif args.model == 'ddqn':
        from ddqn import DDQN as Model
    elif args.model == 'ddpg':
        from ddpg import DDPG as Model
    elif args.model == 'supervised':
        from supervised import Supervised as Model
    elif args.model == 'mcre':
        from mcre import MCRE as Model
    else:
        raise NotImplementedError('Model <%s> not implemented' % args.model)

    def create():
        return Model(config)
    return create


def make_memory(model, buffer_size, state_size, action_size):
    """Initializes a memory structure.

    Some models require slight modifications to the replay buffer,
    such as sampling a full episode, setting discounted rewards, or
    altering the action. in these cases, the base.memory module gets
    overridden in the respective files.
    """

    if model == 'supervised':
        from supervised import Memory
    elif model == 'mcre':
        from mcre import Memory
    else:
        Memory = BaseMemory

    return Memory(buffer_size, state_size, action_size)


def main(args):
    """Main driver for evaluating different models.

    Can be used in both training and testing mode.
    """

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # Make the remote environments; the models aren't very large and can
    # be run fairly quickly on the cpus. Save the GPUs for training
    model_creator = make_model(args, torch.device('cpu'))
    env_creator = make_env(args.max_steps, args.is_test, args.render)

    envs = []
    for _ in range(args.remotes):
        envs.append(GymEnvironment.remote(model_creator, env_creator, seed=None))

    # We'll put the trainable model on the GPU if one's available
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = make_model(args, device)()

    if args.checkpoint is not None:
        model.load_checkpoint(args.checkpoint)

    model_parameters = filter(lambda p: p.requires_grad, model.model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print('Number of parameters: ', params)


    # Train
    if not args.is_test:

        checkpoint_dir = os.path.join('checkpoints', args.model)
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        # Some methods have specialized memory implementations
        memory = make_memory(args.model, args.buffer_size,
                             state_size=(3, 64, 64), action_size=(4))
        memory.load(**vars(args))

        scheduler = torch.optim.lr_scheduler.StepLR(model.optimizer, 6, gamma=0.1)

        # Validate the model every full pass through the data
        iters_per_epoch = args.buffer_size // args.batch_size

        step_queue = deque(maxlen=max(200, args.rollouts * args.remotes))
        reward_queue = deque(maxlen=step_queue.maxlen)
        loss_queue = deque(maxlen=step_queue.maxlen)

        results = []
        start = time.time()
        for episode in range(args.max_episodes):

            loss = model.train(memory, **vars(args))
            loss_queue.append(loss)

            if episode % args.update_iter == 0:
                model.update()

            # Validation step;
            # Here we take the weights from the current network, and distribute
            # them to all remote instances. While the network trains for another
            # epoch, these instances will run in parallel so we can check
            # performance. Note that once a training epoch is done, we will need
            # to wait for the remote instances to finish
            if episode % (iters_per_epoch // 1) == 0:

                ep = '%d' % (episode // iters_per_epoch)
                checkpoint = os.path.join(checkpoint_dir, ep)
                model.save_checkpoint(checkpoint)

                # Collect results from the previous epoch
                for device in ray.get(results):
                    step_queue.extend(device[0])
                    reward_queue.extend(device[1])

                # Send the weights out for current epoch
                results = []
                for env in envs:
                    e = env.rollout.remote(model.get_weights(),
                                           args.rollouts,
                                           args.explore)
                    results.append(e)

                scheduler.step()
                for param_group in model.optimizer.param_groups:
                    param_group['lr'] = max(param_group['lr'], 1e-7)

                print('Epoch: %s, Step: %2.4f, Reward: %1.2f, Loss: %2.4f, Took:%2.4fs'%\
                     (ep, np.mean(step_queue), np.mean(reward_queue),
                      np.mean(loss_queue), time.time() - start))

                start = time.time()

    print('Testing --------')

    results, steps, rewards = [], [], []
    for env in envs:
        e = env.rollout.remote(model.get_weights(),
                               args.rollouts,
                               args.explore)
        results.append(e)

    for out in ray.get(results):
        steps.extend(out[0])
        rewards.extend(out[1])

    print('Average across (%d) episodes: Step: %2.4f, Reward: %1.2f' %
          (args.rollouts * args.remotes, np.mean(steps), np.mean(rewards)))


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Off Policy Deep Q-Learning')

    # Model parameters
    parser.add_argument('--model', default='ddqn')
    parser.add_argument('--data-dir', default='data100K')
    parser.add_argument('--buffer-size', default=100000, type=int)
    parser.add_argument('--checkpoint', default=None)
    parser.add_argument('--epochs', dest='max_episodes', default=200000, type=int)
    parser.add_argument('--explore', default=0.0, type=float)

    # Hyperparameters
    parser.add_argument('--seed', default=1234, type=int)
    parser.add_argument('--channels', dest='out_channels', default=32, type=int)
    parser.add_argument('--gamma', default=0.90, type=float)
    parser.add_argument('--decay', default=1e-5, type=float)
    parser.add_argument('--lr', dest='lrate', default=1e-3, type=float)
    parser.add_argument('--batch-size', default=128, type=int)
    parser.add_argument('--update', dest='update_iter', default=50, type=int)
    parser.add_argument('--uniform', dest='num_uniform', default=16, type=int)
    parser.add_argument('--cem', dest='num_cem', default=64, type=int)
    parser.add_argument('--cem-iter', default=5, type=int)
    parser.add_argument('--cem-elite', default=6, type=int)

    # Environment Parameters
    parser.add_argument('--max-steps', default=15, type=int)
    parser.add_argument('--render', action='store_true', default=False)
    parser.add_argument('--test', dest='is_test', action='store_true', default=False)

    # Distributed Parameters
    parser.add_argument('--rollouts', default=8, type=int)
    parser.add_argument('--remotes', default=10, type=int)

    main(parser.parse_args())