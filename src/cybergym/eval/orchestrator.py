"""
Multiprocessing orchestration for CyberGym evaluations.

Manages parallel execution of agents and judges with dynamic task queuing.
"""

import logging
import multiprocessing as mp
import time
from typing import Any, Callable

from tqdm import tqdm

from .types import AgentResult, JudgeResult

logger = logging.getLogger(__name__)


def run_evaluation_pool(
    run_args_list: list[tuple],
    agent_runner: Callable[[tuple], AgentResult],
    judge_runner: Callable[[tuple], JudgeResult],
    parallel_requests: int,
    is_re_mode: bool,
    num_of_judges: int,
    make_judge_args: Callable[[str, str, int, int], tuple],
) -> tuple[list[AgentResult], list[JudgeResult]]:
    """
    Execute agents and judges with parallel processing.

    Args:
        run_args_list: List of argument tuples for agent runs
        agent_runner: Function to run a single agent (accepts tuple, returns AgentResult)
        judge_runner: Function to run a single judge (accepts tuple, returns JudgeResult)
        parallel_requests: Number of parallel workers
        is_re_mode: Whether this is reverse engineering mode (enables judge runs)
        num_of_judges: Number of judge evaluations per submission
        make_judge_args: Factory function to create judge args tuple
            Signature: (task_id, agent_id, run_num, judge_num) -> tuple

    Returns:
        Tuple of (agent_results, judge_results)
    """
    if parallel_requests > 1:
        return _run_parallel(
            run_args_list,
            agent_runner,
            judge_runner,
            parallel_requests,
            is_re_mode,
            num_of_judges,
            make_judge_args,
        )
    else:
        return _run_sequential(
            run_args_list,
            agent_runner,
            judge_runner,
            is_re_mode,
            num_of_judges,
            make_judge_args,
        )


def _run_parallel(
    run_args_list: list[tuple],
    agent_runner: Callable[[tuple], AgentResult],
    judge_runner: Callable[[tuple], JudgeResult],
    parallel_requests: int,
    is_re_mode: bool,
    num_of_judges: int,
    make_judge_args: Callable[[str, str, int, int], tuple],
) -> tuple[list[AgentResult], list[JudgeResult]]:
    """Run agents and judges in parallel using multiprocessing pool."""
    logger.info(f"Using multiprocessing with {parallel_requests} workers")

    agent_results: list[AgentResult] = []
    judge_results: list[JudgeResult] = []

    with mp.Pool(parallel_requests) as pool:
        agent_futures: dict[Any, tuple] = {}
        judge_futures: dict[Any, tuple[str, str, int]] = {}
        agent_queue_index = 0
        total_agents = len(run_args_list)

        # Submit initial batch of agents (up to pool size)
        for _ in range(min(parallel_requests, total_agents)):
            run_args = run_args_list[agent_queue_index]
            future = pool.apply_async(agent_runner, (run_args,))
            agent_futures[future] = run_args
            agent_queue_index += 1

        completed_agents = 0

        with tqdm(total=total_agents, desc="Running agents") as pbar:
            # Poll for completed agents and submit new ones
            while completed_agents < total_agents or judge_futures:
                # Check agent completions
                for future in list(agent_futures.keys()):
                    if future.ready():
                        try:
                            result = future.get()
                            task_id, run_num, success, error, agent_id = result
                            agent_results.append(result)
                            completed_agents += 1
                            pbar.update(1)

                            # Queue judges if RE mode and agent succeeded
                            if is_re_mode and success and agent_id:
                                for judge_num in range(num_of_judges):
                                    judge_args = make_judge_args(task_id, agent_id, run_num, judge_num)
                                    judge_future = pool.apply_async(judge_runner, (judge_args,))
                                    judge_futures[judge_future] = (task_id, agent_id, judge_num)
                                logger.info(f"Queued {num_of_judges} judges for {task_id} agent {agent_id}")

                        except Exception as e:
                            logger.error(f"Error getting agent result: {e}")
                            completed_agents += 1
                            pbar.update(1)

                        del agent_futures[future]

                        # Submit next agent if any remain
                        if agent_queue_index < total_agents:
                            run_args = run_args_list[agent_queue_index]
                            new_future = pool.apply_async(agent_runner, (run_args,))
                            agent_futures[new_future] = run_args
                            agent_queue_index += 1

                # Check judge completions
                for future in list(judge_futures.keys()):
                    if future.ready():
                        try:
                            result = future.get()
                            judge_results.append(result)
                            task_id, agent_id, judge_num, success, error = result
                            if success:
                                logger.info(f"✓ Judge {judge_num} completed for {task_id} agent {agent_id}")
                            else:
                                logger.warning(f"✗ Judge {judge_num} failed for {task_id} agent {agent_id}: {error}")
                        except Exception as e:
                            logger.error(f"Error getting judge result: {e}")

                        del judge_futures[future]

                time.sleep(0.1)  # Small delay to avoid busy-waiting

    return agent_results, judge_results


def _run_sequential(
    run_args_list: list[tuple],
    agent_runner: Callable[[tuple], AgentResult],
    judge_runner: Callable[[tuple], JudgeResult],
    is_re_mode: bool,
    num_of_judges: int,
    make_judge_args: Callable[[str, str, int, int], tuple],
) -> tuple[list[AgentResult], list[JudgeResult]]:
    """Run agents and judges sequentially."""
    logger.info("Running agents sequentially")

    agent_results: list[AgentResult] = []
    judge_results: list[JudgeResult] = []

    for run_args in tqdm(run_args_list, desc="Running agents"):
        result = agent_runner(run_args)
        task_id, run_num, success, error, agent_id = result
        agent_results.append(result)

        # Run judges immediately after agent in sequential mode
        if is_re_mode and success and agent_id:
            for judge_num in range(num_of_judges):
                judge_args = make_judge_args(task_id, agent_id, run_num, judge_num)
                judge_result = judge_runner(judge_args)
                judge_results.append(judge_result)

    return agent_results, judge_results
