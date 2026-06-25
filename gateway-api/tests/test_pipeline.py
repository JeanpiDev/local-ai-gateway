"""Núcleo del pipeline: PolicyStructure, short-circuit y fail_mode."""
import pytest

from app.policy import Policy, Roles, Limits
from app.security.pipeline import GuardBlocked, GuardContext, GuardPipeline, Stage, StageAction, StageResult
from app.security.stages import PolicyStructureStage


def test_prepends_server_system_and_drops_client_system():
    policy = Policy(system_prompt="SP-SERVIDOR", roles=Roles(allowed=["user", "assistant"], drop_client_system=True))
    ctx = GuardContext(messages=[
        {"role": "system", "content": "system del cliente"},
        {"role": "user", "content": "hola"},
    ])
    out = GuardPipeline([PolicyStructureStage(policy)]).run(ctx)
    assert out[0] == {"role": "system", "content": "SP-SERVIDOR"}
    assert all(m["content"] != "system del cliente" for m in out)
    assert out[-1] == {"role": "user", "content": "hola"}


def test_role_not_allowed_blocks():
    policy = Policy(roles=Roles(allowed=["user"]))
    ctx = GuardContext(messages=[{"role": "tool", "content": "x"}])
    with pytest.raises(GuardBlocked):
        GuardPipeline([PolicyStructureStage(policy)]).run(ctx)


def test_max_chars_limit_blocks():
    policy = Policy(limits=Limits(max_chars_per_message=5))
    ctx = GuardContext(messages=[{"role": "user", "content": "demasiado largo"}])
    with pytest.raises(GuardBlocked):
        GuardPipeline([PolicyStructureStage(policy)]).run(ctx)


class _Block(Stage):
    name = "Blocker"
    def run(self, ctx):
        return StageResult(action=StageAction.BLOCK, reason="t", detail={"x": 1})


class _Boom(Stage):
    def __init__(self, fail_mode):
        self.name = "Boom"
        self.fail_mode = fail_mode
    def run(self, ctx):
        raise RuntimeError("boom")


def test_short_circuit_raises_guardblocked():
    with pytest.raises(GuardBlocked) as e:
        GuardPipeline([_Block()]).run(GuardContext(messages=[{"role": "user", "content": "x"}]))
    assert e.value.stage == "Blocker"


def test_fail_closed_blocks_on_exception():
    with pytest.raises(GuardBlocked):
        GuardPipeline([_Boom("closed")]).run(GuardContext(messages=[{"role": "user", "content": "x"}]))


def test_fail_open_passes_on_exception():
    msgs = [{"role": "user", "content": "x"}]
    out = GuardPipeline([_Boom("open")]).run(GuardContext(messages=msgs))
    assert out == msgs
