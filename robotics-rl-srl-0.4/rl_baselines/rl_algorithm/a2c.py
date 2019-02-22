import os
import pickle
import time

import numpy as np
import tensorflow as tf
from baselines.acer.acer_simple import find_trainable_variables, joblib
from baselines.common import tf_util
from baselines.a2c.a2c import discount_with_dones, explained_variance, Model
from baselines import logger

from rl_baselines.base_classes import BaseRLObject
from rl_baselines.policies import PPO2MLPPolicy, PPO2CNNPolicy
from rl_baselines.utils import createTensorflowSession


class A2CModel(BaseRLObject):
    """
    object containing the interface between baselines.a2c and this code base
    A2C: A synchronous, deterministic variant of Asynchronous Advantage Actor Critic (A3C)
    """

    SAVE_INTERVAL = 10  # Save RL model every 10 steps

    def __init__(self):
        super(A2CModel, self).__init__()
        self.ob_space = None
        self.ac_space = None
        self.policy = None
        self.model = None
        self.states = None

    def save(self, save_path, _locals=None):
        assert self.model is not None, "Error: must train or load model before use"
        self.model.save(os.path.dirname(save_path) + "/a2c_weights.pkl")
        save_param = {
            "ob_space": self.ob_space,
            "ac_space": self.ac_space,
            "policy": self.policy
        }
        with open(save_path, "wb") as f:
            pickle.dump(save_param, f)

    @classmethod
    def load(cls, load_path, args=None):
        sess = tf_util.make_session()

        with open(load_path, "rb") as f:
            save_param = pickle.load(f)
        loaded_model = A2CModel()
        loaded_model.__dict__ = {**loaded_model.__dict__, **save_param}

        # MLP: multi layer perceptron
        # CNN: convolutional neural netwrok
        # LSTM: Long Short Term Memory
        # LNLSTM: Layer Normalization LSTM
        policy = {'cnn': PPO2CNNPolicy(),
                  'cnn-lstm': PPO2CNNPolicy(reccurent=True),
                  'cnn-lnlstm': PPO2CNNPolicy(reccurent=True, normalised=True),
                  'mlp': PPO2MLPPolicy(),
                  'lstm': PPO2MLPPolicy(reccurent=True),
                  'lnlstm': PPO2MLPPolicy(reccurent=True, normalised=True)}[loaded_model.policy]
        loaded_model.model = policy(sess, loaded_model.ob_space, loaded_model.ac_space, args.num_cpu, nsteps=1,
                                    reuse=False)
        loaded_model.states = loaded_model.model.initial_state

        tf.global_variables_initializer().run(session=sess)
        loaded_params = joblib.load(os.path.dirname(load_path) + "/a2c_weights.pkl")
        restores = []
        for p, loaded_p in zip(find_trainable_variables("model"), loaded_params):
            restores.append(p.assign(loaded_p))
        sess.run(restores)

        return loaded_model

    def customArguments(self, parser):
        parser.add_argument('--num-cpu', help='Number of processes', type=int, default=1)
        parser.add_argument('--policy', help='Policy architecture', choices=['feedforward', 'lstm', 'lnlstm'],
                            default='feedforward')
        parser.add_argument('--lr-schedule', help='Learning rate schedule', choices=['constant', 'linear'],
                            default='constant')
        return parser

    def getActionProba(self, observation, dones=None):
        assert self.model is not None, "Error: must train or load model before use"
        return self.model.probaStep(observation, self.states, dones)

    def getAction(self, observation, dones=None):
        assert self.model is not None, "Error: must train or load model before use"
        actions, _, self.states, _ = self.model.step(observation, self.states, dones)
        return actions

    @classmethod
    def getOptParam(cls):
        return {
            "nsteps": (int, (1, 100)),
            "vf_coef": (float, (0, 1)),
            "ent_coef": (float, (0, 1)),
            "max_grad_norm": (float, (0.1, 5)),
            "lr": (float, (0, 0.1)),
            "epsilon": (float, (0, 0.01)),
            "alpha": (float, (0.5, 1)),
            "gamma": (float, (0.5, 1)),
            "lrschedule": ((list, str), ['linear', 'constant', 'double_linear_con', 'middle_drop', 'double_middle_drop'])
        }

    def train(self, args, callback, env_kwargs=None, hyperparam=None):
        envs = self.makeEnv(args, env_kwargs=env_kwargs)

        # get the associated policy for the architecture requested
        if args.srl_model == "raw_pixels":
            if args.policy == "feedforward":
                args.policy = "cnn"
            else:
                args.policy = "cnn-" + args.policy
        else:
            if args.policy == "feedforward":
                args.policy = "mlp"

        self.policy = args.policy
        self.ob_space = envs.observation_space
        self.ac_space = envs.action_space

        learn_param = {
            "lrschedule": args.lr_schedule
        }

        # set hyperparameters
        hyperparam = self.parserHyperParam(hyperparam)
        learn_param.update(hyperparam)

        logger.configure()
        self._learn(args.policy, envs, total_timesteps=args.num_timesteps, seed=args.seed, callback=callback,
                    **learn_param)
        envs.close()

    def _learn(self, policy, env, seed=0, nsteps=5, total_timesteps=int(1e6), vf_coef=0.5, ent_coef=0.05,
               max_grad_norm=0.5, lr=7e-4, lrschedule='linear', epsilon=1e-5, alpha=0.99, gamma=0.99, log_interval=100,
               callback=None):
        """
        Return a trained A2C model.

        :param policy: (A2CPolicy) The policy model to use (MLP, CNN, LSTM, ...)
        :param env: (Gym environment) The environment to learn from
        :param seed: (int) The initial seed for training
        :param nsteps: (int) The number of steps to run for each environment
        :param total_timesteps: (int) The total number of samples
        :param vf_coef: (float) Value function coefficient for the loss calculation
        :param ent_coef: (float) Entropy coefficient for the loss caculation
        :param max_grad_norm: (float) The maximum value for the gradiant clipping
        :param lr: (float) The learning rate
        :param lrschedule: (str) The type of scheduler for the learning rate update ('linear', 'constant',
                                     'double_linear_con', 'middle_drop' or 'double_middle_drop')
        :param epsilon: (float) RMS prop optimizer epsilon
        :param alpha: (float) RMS prop optimizer decay
        :param gamma: (float) Discount factor
        :param log_interval: (int) The number of timesteps before logging.
        :return: (Model) A2C model
        """
        tf.reset_default_graph()
        createTensorflowSession()

        # MLP: multi layer perceptron
        # CNN: convolutional neural netwrok
        # LSTM: Long Short Term Memory
        # LNLSTM: Layer Normalization LSTM
        policy_fn = {'cnn': PPO2CNNPolicy(),
                     'cnn-lstm': PPO2CNNPolicy(reccurent=True),
                     'cnn-lnlstm': PPO2CNNPolicy(reccurent=True, normalised=True),
                     'mlp': PPO2MLPPolicy(),
                     'lstm': PPO2MLPPolicy(reccurent=True),
                     'lnlstm': PPO2MLPPolicy(reccurent=True, normalised=True)}[policy]

        nenvs = env.num_envs
        ob_space = env.observation_space
        ac_space = env.action_space
        self.model = Model(policy=policy_fn, ob_space=ob_space, ac_space=ac_space, nenvs=nenvs, nsteps=nsteps,
                           ent_coef=ent_coef,
                           vf_coef=vf_coef,
                           max_grad_norm=max_grad_norm, lr=lr, alpha=alpha, epsilon=epsilon,
                           total_timesteps=total_timesteps,
                           lrschedule=lrschedule)
        self.states = self.model.initial_state
        runner = _Runner(env, self.model, nsteps=nsteps, gamma=gamma)

        nbatch = nenvs * nsteps
        tstart = time.time()
        for update in range(1, total_timesteps // nbatch + 1):
            obs, states, rewards, masks, actions, values = runner.run()
            policy_loss, value_loss, policy_entropy = self.model.train(obs, states, rewards, masks, actions, values)
            nseconds = time.time() - tstart
            fps = int((update * nbatch) / nseconds)

            if callback is not None:
                callback(locals(), globals())

            if update % log_interval == 0 or update == 1:
                ev = explained_variance(values, rewards)
                logger.record_tabular("nupdates", update)
                logger.record_tabular("total_timesteps", update * nbatch)
                logger.record_tabular("fps", fps)
                logger.record_tabular("policy_entropy", float(policy_entropy))
                logger.record_tabular("value_loss", float(value_loss))
                logger.record_tabular("explained_variance", float(ev))
                logger.dump_tabular()


# Redefine runner to add support for srl models
class _Runner(object):
    def __init__(self, env, model, nsteps=5, gamma=0.99):
        self.env = env
        self.model = model
        nenv = env.num_envs
        if len(env.observation_space.shape) > 1:
            nh, nw, nc = env.observation_space.shape
            self.batch_ob_shape = (nenv * nsteps, nh, nw, nc)
            self.obs_dtype = np.uint8
            self.obs = np.zeros((nenv, nh, nw, nc), dtype=self.obs_dtype)
            self.nc = nc
        else:
            obs_dim = env.observation_space.shape[0]
            self.batch_ob_shape = (nenv * nsteps, obs_dim)
            self.obs_dtype = np.float32
            self.obs = np.zeros((nenv, obs_dim), dtype=self.obs_dtype)

        env.reset()
        self.gamma = gamma
        self.nsteps = nsteps
        self.states = model.initial_state
        self.dones = [False for _ in range(nenv)]

    def run(self):
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones = [], [], [], [], []
        mb_states = self.states
        for n in range(self.nsteps):
            actions, values, states, _ = self.model.step(self.obs, self.states, self.dones)
            mb_obs.append(np.copy(self.obs))
            mb_actions.append(actions)
            mb_values.append(values)
            mb_dones.append(self.dones)
            obs, rewards, dones, _ = self.env.step(actions)
            self.states = states
            self.dones = dones
            for k, done in enumerate(dones):
                if done:
                    self.obs[k] = self.obs[k] * 0
            self.obs = obs
            mb_rewards.append(rewards)
        mb_dones.append(self.dones)
        # batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=self.obs_dtype).swapaxes(1, 0).reshape(self.batch_ob_shape)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32).swapaxes(1, 0)
        mb_actions = np.asarray(mb_actions, dtype=np.int32).swapaxes(1, 0)
        mb_values = np.asarray(mb_values, dtype=np.float32).swapaxes(1, 0)
        mb_dones = np.asarray(mb_dones, dtype=np.bool).swapaxes(1, 0)
        mb_masks = mb_dones[:, :-1]
        mb_dones = mb_dones[:, 1:]
        last_values = self.model.value(self.obs, self.states, self.dones).tolist()
        # discount/bootstrap off value fn
        for n, (rewards, dones, value) in enumerate(zip(mb_rewards, mb_dones, last_values)):
            rewards = rewards.tolist()
            dones = dones.tolist()
            if dones[-1] == 0:
                rewards = discount_with_dones(rewards + [value], dones + [0], self.gamma)[:-1]
            else:
                rewards = discount_with_dones(rewards, dones, self.gamma)
            mb_rewards[n] = rewards
        mb_rewards = mb_rewards.flatten()
        mb_actions = mb_actions.flatten()
        mb_values = mb_values.flatten()
        mb_masks = mb_masks.flatten()
        return mb_obs, mb_states, mb_rewards, mb_masks, mb_actions, mb_values
