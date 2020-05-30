import gym
import wandb

from rl_credit.environment import (
    DISCOUNT_TIMESCALE,
    DISCOUNT_FACTOR,
    VaryGiftsGoalEnv,
)
from rl_credit.examples.train import train


####################################################
# Environments: Time delays in distractor phase

class Delay0_Gifts(VaryGiftsGoalEnv):
    def __init__(self):
        distractor_xtra_kwargs = {'max_steps': 0.0 * DISCOUNT_TIMESCALE}
        super().__init__(distractor_xtra_kwargs)


class Delay0_5_Gifts(VaryGiftsGoalEnv):
    def __init__(self):
        distractor_xtra_kwargs = {'max_steps': 0.5 * DISCOUNT_TIMESCALE}
        super().__init__(distractor_xtra_kwargs)



####################################################
# Config params shared among all experiments

common_train_config = dict(
    num_procs=16,
    save_interval=300,
    total_frames=16*600*300,  #2_880_000
    log_interval=1,
)

common_algo_kwargs = dict(
    num_frames_per_proc=600,
    discount=DISCOUNT_FACTOR,
    lr=0.001,
    gae_lambda=0.95,
    entropy_coef=0.01,
    value_loss_coef=0.5,
    max_grad_norm=0.5,
    rmsprop_alpha=0.99,
    rmsprop_eps=1e-8,
    reshape_reward=None,
)

# Number of runs to average over per experiment
seeds = range(5)

####################################################
# Experiment-specific configs

# experiment 1
model_dir_stem='a2c_mem10_giftdelay0'
expt_train_config = dict(
    env_id='GiftDistractorDelay0-v0',
    algo_name='a2c',
    recurrence=10,
)
expt_algo_kwargs = {}

# wandb metadata (tags, notes)
delay_factor = 'delay_factor=0'
delay_steps = 'delay_steps=0'
wandb_notes = 'A2C with recurrence=10, gift env delay=0'


# experiment 2
# expt_train_config = dict(
#     env=Delay0_5Gifts,
#     model_name='',
#     model_dir='',
#     algo_name='a2c',
#     recurrence=10,
# )
# delay_factor = 'delay_factor=0.5'
# delay_steps = 'delay_steps=50'


def main(seed):
    wandb_params = {}
    algo_kwargs = common_algo_kwargs
    algo_kwargs.update(expt_algo_kwargs)

    train_config = common_train_config
    expt_train_config['model_dir_stem'] = f"{model_dir_stem}_seed{seed}"
    train_config.update(expt_train_config)
    train_config['seed'] = seed

    train_config.update({'algo_kwargs': algo_kwargs})

    # expt run params to record in wandb
    wandb_params.update(train_config)
    wandb_params.update(common_algo_kwargs)
    wandb_params.update({'env_params': str(vars(gym.make(train_config['env_id'])))})
    wandb_name = f"{train_config['algo_name']}|mem={train_config['recurrence']}|{train_config['env_id']}"

    wandb.init(
        project="distractor_time_delays",
        config=wandb_params,
        name=wandb_name,
        tags=[train_config['algo_name'],
              delay_factor,
              delay_steps,
              train_config['env_id'],
              f'discount_timescale={DISCOUNT_TIMESCALE}'],
        notes=wandb_notes,
        reinit=True,
        group=wandb_name,
        job_type='training'
    )

    wandb_dir = wandb.run.dir
    train_config.update({'wandb_dir': wandb_dir})
    train(**train_config)

    wandb.join()


if __name__ == '__main__':
    for seed in seeds:
        main(seed)