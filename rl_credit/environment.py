from gym_minigrid.envs.delayed_reward_multiphase import ThreePhaseDelayedReward
from gym_minigrid.envs.opengifts import GiftsEnv
from gym_minigrid.envs.goalkeyoptional import GoalKeyOptionalEnv


# gamma = 0.99
DISCOUNT_FACTOR = 0.99
DISCOUNT_TIMESCALE = 100

STEPS_IN_PHASE1 = 50


class KeyGiftsGoalBaseEnv(ThreePhaseDelayedReward):

    def __init__(self, distractor_kwargs):
        super().__init__(
            key_kwargs=dict(
                size=6,
                key_color='yellow',
                start_by_key=False,
                max_steps=STEPS_IN_PHASE1,
                done_when_fetched=False,
            ),
            distractor_kwargs=distractor_kwargs,
            distractor_env=GiftsEnv,
            delayed_reward_env=GoalKeyOptionalEnv,
            delayed_reward_kwargs=dict(
                size=8,
                key_color=None,
                max_steps=2*8**2,
                goal_reward=5.,
                key_reward=20.,
            )
        )


class VaryGiftsGoalEnv(KeyGiftsGoalBaseEnv):

    def __init__(self, distractor_xtra_kwargs):

        assert 'max_steps' in distractor_xtra_kwargs.keys()

        distractor_kwargs=dict(
            size=6,
            num_objs=4,
            gift_reward=5,
            max_steps=0.5*DISCOUNT_TIMESCALE,
            done_when_all_opened=False,
        )
        distractor_kwargs.update(distractor_xtra_kwargs)

        super().__init__(distractor_kwargs=distractor_kwargs)


####################################################
# Reward size in distractor phase

class Mean0_Gifts(VaryGiftsGoalEnv):
    distractor_xtra_kwargs = {'gift_reward': 0}


####################################################
# Reward variance in distractor phase

class Var0_Gifts(VaryGiftsGoalEnv):
    distractor_xtra_kwargs = {'gift_reward': [5, 5]}


class Var2_Gifts(VaryGiftsGoalEnv):
    distractor_xtra_kwargs = {'gift_reward': [3, 7]}
