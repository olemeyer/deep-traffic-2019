#!/usr/bin/env python3
import argparse
import logging

import sys
import ptan
import pathlib
import numpy as np
import gym.wrappers
from tensorboardX import SummaryWriter
from libtraffic import env, utils, model, config

import torch
import torch.optim as optim

log = logging.getLogger("train_model")


def test_agent(ini, net, steps=1000, rounds=5, device=torch.device('cpu')):
    round_means = []
    for _ in range(rounds):
        speed_hist = []
        test_env = env.DeepTraffic(lanes_side=ini.env_lanes_side, patches_ahead=ini.env_patches_ahead,
                                   patches_behind=ini.env_patches_behind, history=ini.env_history, obs=ini.env_obs)
        obs = test_env.reset()

        for _ in range(steps):
            speed_hist.append(test_env.current_speed())
            obs_v = torch.tensor([obs]).to(device)
            q_v = net(obs_v)[0]
            act_idx = torch.argmax(q_v).item()
            obs, reward, _, _ = test_env.step(act_idx)
        round_means.append(np.mean(speed_hist))
    return np.mean(round_means), np.std(round_means)


if __name__ == "__main__":
    utils.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    parser.add_argument("-i", '--ini', required=True, help="Name of ini file to use")
    parser.add_argument("--show-model", default=False, action='store_true', help="Display model and exit")
    args = parser.parse_args()
    ini = config.Settings(args.ini)

    device = torch.device("cuda" if ini.train_cuda else "cpu")

    if not args.show_model:
        name = pathlib.Path(args.ini).stem + "-" + args.name
        save_path = pathlib.Path("saves") / name
        save_path.mkdir(parents=True, exist_ok=True)
        writer = SummaryWriter(comment="-" + name)

    e = env.DeepTraffic(lanes_side=ini.env_lanes_side, patches_ahead=ini.env_patches_ahead,
                        patches_behind=ini.env_patches_behind, history=ini.env_history, obs=ini.env_obs)
    orig_env = e
    obs_shape = e.obs_shape
    e = gym.wrappers.TimeLimit(e, max_episode_steps=ini.env_steps_limit)

    log.info("Environment created, obs shape %s", obs_shape)
    model_class = model.MODELS[ini.train_model]
    net = model_class(obs_shape, e.action_space.n).to(device)
    log.info("Model: %s", net)

    if args.show_model:
        sys.exit(0)

    tgt_net = ptan.agent.TargetNet(net)
    selector = ptan.actions.EpsilonGreedyActionSelector(epsilon=ini.train_eps_start)
    agent = ptan.agent.DQNAgent(net, selector, device=device)
    exp_source = ptan.experience.ExperienceSourceFirstLast(e, agent, gamma=ini.train_gamma, steps_count=1)
    buffer = ptan.experience.ExperienceReplayBuffer(exp_source, buffer_size=ini.train_replay_size)
    optimizer = optim.RMSprop(net.parameters(), lr=ini.train_lr, weight_decay=ini.train_l2_reg)
    sched = optim.lr_scheduler.StepLR(optimizer, 20000, 0.99)
    step = 0
    losses = []
    best_test_speed = None
    with ptan.common.utils.RewardTracker(writer) as tracker:
        while True:
            buffer.populate(1)
            if len(buffer) < ini.train_replay_initial:
                continue
            step += 1
            sched.step()
            selector.epsilon = max(ini.train_eps_end, ini.train_eps_start - step / ini.train_eps_steps)
            new_rewards = exp_source.pop_total_rewards()
            if new_rewards:
                tracker.reward(new_rewards[-1], step, epsilon=selector.epsilon)
                writer.add_scalar("car_speed_train", orig_env.prev_mean_speed, step)
            optimizer.zero_grad()
            batch = buffer.sample(ini.train_batch_size)
            loss_v = model.calc_loss_dqn(batch, net, tgt_net.target_model, gamma=ini.train_gamma, device=device)
            loss_v.backward()
            losses.append(loss_v.item())
            optimizer.step()

            if step % ini.train_net_sync_steps == 0:
                tgt_net.sync()
            if step % ini.train_test_interval == 0:
                mean_loss = np.mean(losses)
                losses.clear()
                car_speed_mu, car_speed_std = test_agent(ini, net, device=device, rounds=ini.train_test_rounds)
                log.info("%d: loss=%.3f, test_speed_mu=%.2f, test_speed_std=%.2f", step, mean_loss, car_speed_mu,
                         car_speed_std)
                writer.add_scalar("loss", mean_loss, step)
                writer.add_scalar("car_speed_mu", car_speed_mu, step)
                writer.add_scalar("car_speed_std", car_speed_std, step)
                writer.add_scalar("lr", sched.get_lr()[0], step)

                if best_test_speed is None:
                    best_test_speed = car_speed_mu
                elif best_test_speed < car_speed_mu:
                    log.info("Best speed updated: %.2f -> %.2f", best_test_speed, car_speed_mu)
                    best_test_speed = car_speed_mu
                    torch.save(net.state_dict(), save_path / ("best_%.2f.dat" % best_test_speed))

                if ini.train_add_steps_limit_slope > 0:
                    extra_steps = min(int(step * ini.train_add_steps_limit_slope),
                                      ini.train_add_steps_limit_max)
                    limit = ini.env_steps_limit + extra_steps
                    writer.add_scalar("steps_limit", limit, step)
                    e._max_episode_steps = limit
    pass
