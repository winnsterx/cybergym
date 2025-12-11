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
    max_run_retries: int = 3,
    retry_delay: int = 60,
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
        max_run_retries: Maximum number of retries for failed agent runs (default: 3)
        retry_delay: Delay in seconds between run retries (default: 60)

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
            max_run_retries,
            retry_delay,
        )
    else:
        return _run_sequential(
            run_args_list,
            agent_runner,
            judge_runner,
            is_re_mode,
            num_of_judges,
            make_judge_args,
            max_run_retries,
            retry_delay,
        )


def _run_parallel(
    run_args_list: list[tuple],
    agent_runner: Callable[[tuple], AgentResult],
    judge_runner: Callable[[tuple], JudgeResult],
    parallel_requests: int,
    is_re_mode: bool,
    num_of_judges: int,
    make_judge_args: Callable[[str, str, int, int], tuple],
    max_run_retries: int = 3,
    retry_delay: int = 60,
) -> tuple[list[AgentResult], list[JudgeResult]]:
    """Run agents and judges in parallel using multiprocessing pool with retry support."""
    logger.info(f"Using multiprocessing with {parallel_requests} workers")
    logger.info(f"Retry settings: max_retries={max_run_retries}, delay={retry_delay}s")

    agent_results: list[AgentResult] = []
    judge_results: list[JudgeResult] = []

    with mp.Pool(parallel_requests) as pool:
        # Track futures with their args and retry count
        # Format: {future: (run_args, retry_count)}
        agent_futures: dict[Any, tuple[tuple, int]] = {}
        judge_futures: dict[Any, tuple[str, str, int]] = {}
        agent_queue_index = 0
        total_agents = len(run_args_list)

        # Track retry queue: list of (run_args, retry_count, next_retry_time)
        retry_queue: list[tuple[tuple, int, float]] = []

        # Submit initial batch of agents (up to pool size)
        for _ in range(min(parallel_requests, total_agents)):
            run_args = run_args_list[agent_queue_index]
            future = pool.apply_async(agent_runner, (run_args,))
            agent_futures[future] = (run_args, 0)  # retry_count = 0
            agent_queue_index += 1

        completed_agents = 0
        total_retries = 0

        with tqdm(total=total_agents, desc="Running agents") as pbar:
            # Poll for completed agents and submit new ones
            while completed_agents < total_agents or judge_futures or retry_queue:
                current_time = time.time()

                # Check if any retries are ready to be submitted
                ready_retries = [(args, count, t) for args, count, t in retry_queue if t <= current_time]
                for args, count, _ in ready_retries:
                    retry_queue.remove((args, count, _))
                    # Only submit if we have capacity
                    if len(agent_futures) < parallel_requests:
                        task_id = args[0]
                        run_num = args[1]
                        logger.info(f"🔄 Retrying {task_id} run {run_num} (attempt {count + 1}/{max_run_retries})")
                        future = pool.apply_async(agent_runner, (args,))
                        agent_futures[future] = (args, count)
                    else:
                        # Put back in queue if no capacity
                        retry_queue.append((args, count, current_time + 1))

                # Check agent completions
                for future in list(agent_futures.keys()):
                    if future.ready():
                        run_args, retry_count = agent_futures[future]
                        try:
                            result = future.get()
                            task_id, run_num, success, error, agent_id = result

                            if success:
                                # Success - record result
                                agent_results.append(result)
                                completed_agents += 1
                                pbar.update(1)

                                # Queue judges if RE mode and agent succeeded
                                if is_re_mode and agent_id:
                                    for judge_num in range(num_of_judges):
                                        judge_args = make_judge_args(task_id, agent_id, run_num, judge_num)
                                        judge_future = pool.apply_async(judge_runner, (judge_args,))
                                        judge_futures[judge_future] = (task_id, agent_id, judge_num)
                                    logger.info(f"Queued {num_of_judges} judges for {task_id} agent {agent_id}")
                            else:
                                # Failed - check if we should retry
                                is_retryable = _is_retryable_error(error)
                                next_retry = retry_count + 1

                                if is_retryable and next_retry < max_run_retries:
                                    # Schedule retry with delay
                                    retry_time = current_time + retry_delay
                                    retry_queue.append((run_args, next_retry, retry_time))
                                    total_retries += 1
                                    logger.warning(f"⚠️ {task_id} run {run_num} failed (attempt {retry_count + 1}), "
                                                   f"scheduling retry in {retry_delay}s. Error: {error[:100] if error else 'unknown'}")
                                else:
                                    # No more retries - record failure
                                    if next_retry >= max_run_retries:
                                        logger.error(f"✗ {task_id} run {run_num} failed after {max_run_retries} attempts: {error}")
                                    else:
                                        logger.error(f"✗ {task_id} run {run_num} failed (non-retryable): {error}")
                                    agent_results.append(result)
                                    completed_agents += 1
                                    pbar.update(1)

                        except Exception as e:
                            logger.error(f"Error getting agent result: {e}")
                            # Check if we should retry on exception
                            next_retry = retry_count + 1
                            if next_retry < max_run_retries:
                                retry_time = current_time + retry_delay
                                retry_queue.append((run_args, next_retry, retry_time))
                                total_retries += 1
                                logger.warning(f"⚠️ Agent exception, scheduling retry in {retry_delay}s: {e}")
                            else:
                                completed_agents += 1
                                pbar.update(1)

                        del agent_futures[future]

                        # Submit next agent if any remain in original queue
                        if agent_queue_index < total_agents and len(agent_futures) < parallel_requests:
                            run_args = run_args_list[agent_queue_index]
                            new_future = pool.apply_async(agent_runner, (run_args,))
                            agent_futures[new_future] = (run_args, 0)
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

        if total_retries > 0:
            logger.info(f"📊 Total retries performed: {total_retries}")

    return agent_results, judge_results


def _is_retryable_error(error: str | None) -> bool:
    """Check if an error is retryable (rate limits, timeouts, etc.)."""
    if not error:
        return True  # Unknown errors are retryable

    error_lower = error.lower()
    retryable_patterns = [
        "rate_limit",
        "rate limit",
        "ratelimit",
        "timeout",
        "timed out",
        "connection",
        "network",
        "temporary",
        "429",  # HTTP Too Many Requests
        "503",  # Service Unavailable
        "502",  # Bad Gateway
        "500",  # Internal Server Error (sometimes transient)
    ]

    return any(pattern in error_lower for pattern in retryable_patterns)


def _run_sequential(
    run_args_list: list[tuple],
    agent_runner: Callable[[tuple], AgentResult],
    judge_runner: Callable[[tuple], JudgeResult],
    is_re_mode: bool,
    num_of_judges: int,
    make_judge_args: Callable[[str, str, int, int], tuple],
    max_run_retries: int = 3,
    retry_delay: int = 60,
) -> tuple[list[AgentResult], list[JudgeResult]]:
    """Run agents and judges sequentially with retry support."""
    logger.info("Running agents sequentially")
    logger.info(f"Retry settings: max_retries={max_run_retries}, delay={retry_delay}s")

    agent_results: list[AgentResult] = []
    judge_results: list[JudgeResult] = []
    total_retries = 0

    for run_args in tqdm(run_args_list, desc="Running agents"):
        task_id = run_args[0]
        run_num = run_args[1]

        # Retry loop for each agent run
        for attempt in range(max_run_retries):
            try:
                result = agent_runner(run_args)
                _, _, success, error, agent_id = result

                if success:
                    # Success - record and move on
                    agent_results.append(result)

                    # Run judges immediately after agent in sequential mode
                    if is_re_mode and agent_id:
                        for judge_num in range(num_of_judges):
                            judge_args = make_judge_args(task_id, agent_id, run_num, judge_num)
                            judge_result = judge_runner(judge_args)
                            judge_results.append(judge_result)
                    break
                else:
                    # Failed - check if retryable
                    is_retryable = _is_retryable_error(error)

                    if is_retryable and attempt < max_run_retries - 1:
                        total_retries += 1
                        logger.warning(f"⚠️ {task_id} run {run_num} failed (attempt {attempt + 1}/{max_run_retries}), "
                                       f"retrying in {retry_delay}s. Error: {error[:100] if error else 'unknown'}")
                        time.sleep(retry_delay)
                    else:
                        # No more retries or non-retryable error
                        if attempt >= max_run_retries - 1:
                            logger.error(f"✗ {task_id} run {run_num} failed after {max_run_retries} attempts: {error}")
                        else:
                            logger.error(f"✗ {task_id} run {run_num} failed (non-retryable): {error}")
                        agent_results.append(result)
                        break

            except Exception as e:
                if attempt < max_run_retries - 1:
                    total_retries += 1
                    logger.warning(f"⚠️ {task_id} run {run_num} exception (attempt {attempt + 1}), "
                                   f"retrying in {retry_delay}s: {e}")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"✗ {task_id} run {run_num} failed after {max_run_retries} attempts with exception: {e}")
                    # Create a failure result
                    agent_results.append((task_id, run_num, False, str(e), None))
                    break

    if total_retries > 0:
        logger.info(f"📊 Total retries performed: {total_retries}")

    return agent_results, judge_results
