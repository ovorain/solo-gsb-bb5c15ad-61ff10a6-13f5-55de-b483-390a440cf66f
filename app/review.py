"""错题间隔复习（SM-2 简化版）的纯算法部分。

已结束会话中作答时冻结 ``score = 0`` 的错题可生成一份复习计划：创建时把
题干、选项与正确答案从题库**冻结**进计划（之后题库改题/撤回都不影响复习
内容），每题初始熟练因子 ``ease_factor = 2.5``、间隔 0 天、连续成功次数
0，到期日为创建当日（UTC）——初始题目当日到期。

每次复习提交一个 0~5 的整数评分，按 SM-2 规则推进日程：

* 评分 < 3：次日重练（间隔 1 天），连续成功次数清零；
* 评分 >= 3：第 1、2 次成功后的间隔分别为 1、6 天，第 3 次起间隔为
  ``旧间隔 × 熟练因子`` 四舍五入；
* 无论评分高低，每次结果后都按
  ``EF' = max(1.3, EF + 0.1 - (5-q) * (0.08 + (5-q) * 0.02))`` 更新熟练因子。

间隔公式使用的是**更新前**的熟练因子：因子的更新发生在本次结果处理之后
（与经典 SM-2 一致）。注意 Python 内置 ``round`` 是银行家舍入
（``round(2.5) == 2``），这里按「四舍五入」的通常含义实现 half-up。
"""

from __future__ import annotations

import math

INITIAL_EASE_FACTOR = 2.5
MIN_EASE_FACTOR = 1.3
# 评分达到该值算一次「成功」；低于它则次日重练并清零连续成功次数。
PASS_SCORE = 3
FAIL_INTERVAL_DAYS = 1
FIRST_INTERVAL_DAYS = 1
SECOND_INTERVAL_DAYS = 6


def round_half_up(value: float) -> int:
    """四舍五入到整数（0.5 向上进位）；区别于内置 ``round`` 的银行家舍入。"""
    return int(math.floor(value + 0.5))


def next_ease_factor(score: int, ease_factor: float) -> float:
    """``EF' = max(1.3, EF + 0.1 - (5-q)*(0.08 + (5-q)*0.02))``，每次结果后都更新。"""
    gap = 5 - score
    return max(MIN_EASE_FACTOR, ease_factor + 0.1 - gap * (0.08 + gap * 0.02))


def advance(
    score: int, ease_factor: float, interval_days: int, repetitions: int
) -> tuple[float, int, int]:
    """按一次 0~5 的复习评分推进 SM-2 日程。

    返回 ``(新熟练因子, 新间隔天数, 新连续成功次数)``：

    * ``score < 3``：间隔重置为 1 天（次日重练），连续成功次数清零，
      但熟练因子照常按公式更新；
    * 第 1、2 次连续成功：间隔分别为 1、6 天；
    * 第 3 次及以后：``round_half_up(旧间隔 × 旧熟练因子)``（因子在本次
      结果处理之后才更新，故间隔使用更新前的值）。
    """
    new_ease_factor = next_ease_factor(score, ease_factor)
    if score < PASS_SCORE:
        return new_ease_factor, FAIL_INTERVAL_DAYS, 0
    new_repetitions = repetitions + 1
    if new_repetitions == 1:
        new_interval = FIRST_INTERVAL_DAYS
    elif new_repetitions == 2:
        new_interval = SECOND_INTERVAL_DAYS
    else:
        new_interval = round_half_up(interval_days * ease_factor)
    return new_ease_factor, new_interval, new_repetitions
