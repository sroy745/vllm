"""Tests for rejection sampling."""

import pytest
import torch

from vllm.model_executor.layers.typical_acceptance_sampler import (
    TypicalAcceptanceSampler)
from vllm.model_executor.utils import set_random_seed

CUDA_DEVICES = [f"cuda:{i}" for i in range(1)]


def get_zero_temperature_prob_dist(batch_size, k, vocab_size):
    """
    Generates a fake temperature zero probablity distribution.
    Returns:
        1. A fake temperature zero probablity distribution of shape
           [batch_size, k, vocab_size]
        2. Tensor of shape [batch_size, k] containing the token ids 
           of the probability 1.0 tokens at each position.
    """
    # Simulate temperature 0 probability distribution for target probabilities
    # and create target probabilities such that only 1 token id has
    # probability 1.0
    target_probs = torch.rand(batch_size, k, vocab_size, dtype=torch.float32)
    probs = torch.rand(batch_size, k, vocab_size)
    _, zero_temperature_token_ids = torch.max(probs, dim=-1)
    # set the probability of the tokens with ids in zero_temperature_token_ids
    # to 1 and the rest to 0.
    target_probs = torch.zeros_like(probs).scatter_(
        -1, zero_temperature_token_ids.unsqueeze(-1), 1.0)
    return target_probs, zero_temperature_token_ids


def get_draft_token_ids(batch_size: int, k: int, vocab_size: int,
                        token_ids_to_exclude: torch.Tensor):
    """
    Returns a tensor of shape [batch_size, k] of fake draft token ids
    drawn randomly from a vocab of size vocab_size. We however ensure
    that token_ids from token_ids_to_exclude are excluded at the 
    corresponding positions.
    """
    draft_token_ids = torch.empty(batch_size, k, dtype=torch.long)
    for i in range(batch_size):
        for j in range(k):
            # Generate a random token ID excluding token_ids_to_exclude[i, j]
            while True:
                token_id = torch.randint(0, vocab_size, (1, )).item()
                if token_id != token_ids_to_exclude[i, j]:
                    draft_token_ids[i, j] = token_id
                    break
    return draft_token_ids


@pytest.mark.parametrize("k", list(range(1, 6)))
@pytest.mark.parametrize("vocab_size", [30_000, 50_000])
@pytest.mark.parametrize("batch_size", list(range(1, 32)))
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_no_crash_with_varying_dims(k: int, vocab_size: int, batch_size: int,
                                    device: str):
    torch.set_default_device(device)
    typical_acceptance_sampler = TypicalAcceptanceSampler()
    typical_acceptance_sampler.init_gpu_tensors(rank=0)
    target_probs = torch.rand(batch_size, k, vocab_size, dtype=torch.float32)
    bonus_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, 1),
                                    dtype=torch.int64)
    draft_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, k),
                                    dtype=torch.int64)
    # Verify that sampling succeeds for all cases.
    typical_acceptance_sampler(target_probs, bonus_token_ids, draft_token_ids)


@pytest.mark.parametrize("above_or_below_vocab_range", ["above", "below"])
@pytest.mark.parametrize("which_token_ids",
                         ["bonus_token_ids", "draft_token_ids"])
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_raises_when_vocab_oob(above_or_below_vocab_range: str,
                               which_token_ids: str, device: str):
    k = 3
    batch_size = 5
    vocab_size = 30_000
    torch.set_default_device(device)
    typical_acceptance_sampler = TypicalAcceptanceSampler(strict_mode=True)
    typical_acceptance_sampler.init_gpu_tensors(rank=0)
    target_probs = torch.rand(batch_size, k, vocab_size, dtype=torch.float32)
    bonus_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, 1),
                                    dtype=torch.int64)
    draft_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, k),
                                    dtype=torch.int64)
    # Verify that appropriate exceptions are thrown for out
    # of bound vocabs.
    oob_token_ids = None
    if which_token_ids == "bonus_token_ids":
        oob_token_ids = bonus_token_ids
    elif which_token_ids == "draft_token_ids":
        oob_token_ids = draft_token_ids
    else:
        raise AssertionError()

    if above_or_below_vocab_range == "above":
        rogue_token_id = vocab_size + 1
    elif above_or_below_vocab_range == "below":
        rogue_token_id = -1
    else:
        raise AssertionError()

    oob_token_ids[0][0] = rogue_token_id

    with pytest.raises(AssertionError):
        typical_acceptance_sampler(target_probs, bonus_token_ids,
                                   draft_token_ids)


@pytest.mark.parametrize("seed", list(range(10)))
@pytest.mark.parametrize("disable_bonus_tokens", [True, False])
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_uniform_target_distribution_accepts_all_tokens(
        seed: int, disable_bonus_tokens: bool, device: str):
    set_random_seed(seed)
    k = 3
    batch_size = 5
    vocab_size = 30_000
    torch.set_default_device(device)
    typical_acceptance_sampler = TypicalAcceptanceSampler(
        strict_mode=True, disable_bonus_tokens=disable_bonus_tokens)
    typical_acceptance_sampler.init_gpu_tensors(rank=0)
    target_probs = torch.rand(batch_size, k, vocab_size, dtype=torch.float32)
    draft_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, k),
                                    dtype=torch.int64)
    bonus_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, 1),
                                    dtype=torch.int64)
    output_token_ids = typical_acceptance_sampler(target_probs,
                                                  bonus_token_ids,
                                                  draft_token_ids)
    # We are using a uniform target probability distribution.
    # For a uniform distribution the entropy is very high and it
    # should lead to all draft tokens being accepted. Verify that.
    assert output_token_ids.shape[0] == batch_size
    assert output_token_ids.shape[1] == (k + 1)
    if disable_bonus_tokens:
        assert torch.all(output_token_ids[:, -1] == -1)
    else:
        assert torch.all(output_token_ids[:, -1] == bonus_token_ids.squeeze())

    assert torch.all(output_token_ids[:, :k] == draft_token_ids)


@pytest.mark.parametrize("seed", list(range(10)))
@pytest.mark.parametrize("disable_bonus_tokens", [True, False])
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_temperature_zero_target_distribution(seed: int,
                                              disable_bonus_tokens: bool,
                                              device: str):
    set_random_seed(seed)
    k = 3
    batch_size = 5
    vocab_size = 30_000
    torch.set_default_device(device)

    typical_acceptance_sampler = TypicalAcceptanceSampler(
        strict_mode=True, disable_bonus_tokens=disable_bonus_tokens)
    typical_acceptance_sampler.init_gpu_tensors(rank=0)
    # Simulate temperature 0 probability distribution for target probabilities
    # and create target probabilities such that only 1 token id has
    # probability 1.0
    target_probs, zero_temperature_token_ids = get_zero_temperature_prob_dist(
        batch_size, k, vocab_size)
    # Populate draft_token_ids such that they exclude the token_ids
    # with probability = 1.0
    draft_token_ids = get_draft_token_ids(batch_size, k, vocab_size,
                                          zero_temperature_token_ids)
    bonus_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, 1),
                                    dtype=torch.int64)
    # The target probaility distribution is a temperature zero distribution
    # with zero entroy. Since our draft token ids don't match the probability
    # 1.0 tokens in the target distribution we will reject all of them and
    # fallback to the greedy sampling for selecting 1 token for each sequence.
    # Verify the same.
    output_token_ids = typical_acceptance_sampler(target_probs,
                                                  bonus_token_ids,
                                                  draft_token_ids)
    assert output_token_ids.shape[0] == batch_size
    assert output_token_ids.shape[1] == (k + 1)
    assert torch.all(output_token_ids[:, -1] == -1)
    assert torch.all(output_token_ids[:, 0] == zero_temperature_token_ids[:,
                                                                          0])


@pytest.mark.parametrize("seed", list(range(10)))
@pytest.mark.parametrize("disable_bonus_tokens", [True, False])
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_mixed_target_distribution(seed: int, disable_bonus_tokens: bool,
                                   device: str):
    set_random_seed(seed)
    k = 3
    batch_size = 4
    vocab_size = 30_000
    torch.set_default_device(device)
    typical_acceptance_sampler = TypicalAcceptanceSampler(
        strict_mode=True, disable_bonus_tokens=disable_bonus_tokens)
    typical_acceptance_sampler.init_gpu_tensors(rank=0)
    # For sequences 0 and 2 set the distribution to a temperature
    # zero distribution. For sequences 1 and 3 set it to a uniform
    # distribution.
    target_probs, zero_temperature_token_ids = (get_zero_temperature_prob_dist(
        batch_size, k, vocab_size))
    draft_token_ids = get_draft_token_ids(batch_size, k, vocab_size,
                                          zero_temperature_token_ids)
    uniform_probs = torch.rand(2, k, vocab_size, dtype=torch.float32)
    target_probs[[1, 3]] = uniform_probs
    bonus_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, 1),
                                    dtype=torch.int64)
    output_token_ids = typical_acceptance_sampler(target_probs,
                                                  bonus_token_ids,
                                                  draft_token_ids)
    # verify the shape of output_token_ids
    assert output_token_ids.shape[0] == batch_size
    assert output_token_ids.shape[1] == (k + 1)
    # For sequences 0 and 2 verify that only 1 token is accepted
    # which is the token with probability 1.0 in the target distribution
    # at position 0.
    assert torch.all(output_token_ids[[0, 2], 1:] == -1)
    assert (torch.all(output_token_ids[[0, 2],
                                       0] == zero_temperature_token_ids[[0, 2],
                                                                        0]))
    # For sequences 1 and 3 verify that all tokens are accepted since the
    # target probability distribution is uniform. In addition verify that
    # if disable_bonus_tokens is false then we also accept the bonus tokens.
    assert torch.all(
        output_token_ids[[1, 3], :-1] == draft_token_ids[[1, 3], :])
    if disable_bonus_tokens:
        assert torch.all(output_token_ids[[1, 3], -1] == -1)
    else:
        assert torch.all(output_token_ids[[1, 3], -1] != -1)


@pytest.mark.parametrize("seed", list(range(10)))
@pytest.mark.parametrize("disable_bonus_tokens", [True, False])
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_accept_tokens_partially(seed: int, disable_bonus_tokens: bool,
                                 device: str):
    set_random_seed(seed)
    k = 5
    batch_size = 1
    vocab_size = 30_000
    torch.set_default_device(device)
    typical_acceptance_sampler = TypicalAcceptanceSampler(
        strict_mode=True, disable_bonus_tokens=disable_bonus_tokens)
    typical_acceptance_sampler.init_gpu_tensors(rank=0)
    # Create a temperature zero target probability distribution and ensure
    # all draft token ids correspond to the tokens with 1.0 probability.
    # Verify that all of them are accepted.
    target_probs, zero_temperature_token_ids = (get_zero_temperature_prob_dist(
        batch_size, k, vocab_size))
    draft_token_ids = zero_temperature_token_ids
    bonus_token_ids = torch.randint(low=0,
                                    high=vocab_size,
                                    size=(batch_size, 1),
                                    dtype=torch.int64)
    output_token_ids = typical_acceptance_sampler(target_probs,
                                                  bonus_token_ids,
                                                  draft_token_ids)
    assert output_token_ids.shape[0] == batch_size
    assert output_token_ids.shape[1] == (k + 1)
    assert torch.all(output_token_ids[:, 0:-1] == draft_token_ids)
    if disable_bonus_tokens:
        assert torch.all(output_token_ids[:, -1] == -1)
    else:
        assert torch.all(output_token_ids[:, -1] == bonus_token_ids)
    # Next only keep the first 2 draft tokens same as the zero temperature
    # tokens. For the remaining 3 choose some other tokens. In the
    # response we will expect the first 2 tokens to be the same as the
    # draft tokens and the rest as -1
    draft_token_ids_to_replace = get_draft_token_ids(
        batch_size, k, vocab_size, zero_temperature_token_ids)
    draft_token_ids = torch.cat(
        (draft_token_ids[:, :2], draft_token_ids_to_replace[:, -3:]), dim=1)
    output_token_ids = typical_acceptance_sampler(target_probs,
                                                  bonus_token_ids,
                                                  draft_token_ids)
    assert output_token_ids.shape[0] == batch_size
    assert output_token_ids.shape[1] == (k + 1)
    assert torch.all(output_token_ids[:, :2] == draft_token_ids[:, :2])
    assert torch.all(output_token_ids[:, -3:] == -1)


@pytest.mark.parametrize("seed", list(range(10)))
@pytest.mark.parametrize("disable_bonus_tokens", [True, False])
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_replacement_token_ids(
    seed: int, disable_bonus_tokens: bool, device: str):
    set_random_seed(seed)
    k = 10
    batch_size = 5
    vocab_size = 30_000
    torch.set_default_device(device)
    typical_acceptance_sampler = TypicalAcceptanceSampler(
        strict_mode=True, disable_bonus_tokens=disable_bonus_tokens)
    typical_acceptance_sampler.init_gpu_tensors(rank=0)
    target_probs = torch.rand(batch_size, k, vocab_size, dtype=torch.float32)
    expected_replacement_tokens = -torch.ones(
        (batch_size, k), dtype=torch.long)
    expected_replacement_tokens[:, 0] = torch.argmax(
        target_probs[:, 0, :], dim=1)
    actual_replacement_tokens = (
        typical_acceptance_sampler._replacement_token_ids(target_probs)) 
    assert torch.all(expected_replacement_tokens == actual_replacement_tokens)
