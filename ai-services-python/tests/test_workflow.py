from types import SimpleNamespace

import pytest

from app.pipeline.workflow import Step, Workflow, WorkflowError


def ctx():
    return SimpleNamespace(job_id="job_test", log=[])


def test_steps_run_in_order():
    c = ctx()
    Workflow("t", [Step("a", lambda x: x.log.append("a")), Step("b", lambda x: x.log.append("b"))]).run(c)
    assert c.log == ["a", "b"]


def test_retries_then_succeeds_with_exponential_backoff():
    c = ctx()
    sleeps = []
    attempts = {"n": 0}

    def flaky(_):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")

    Workflow("t", [Step("flaky", flaky, retries=2, backoff_seconds=1.0)], sleep=sleeps.append).run(c)
    assert attempts["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_failure_compensates_completed_steps_in_reverse_and_skips_rest():
    c = ctx()

    def boom(_):
        raise RuntimeError("boom")

    wf = Workflow("t", [
        Step("a", lambda x: x.log.append("a"), compensate=lambda x: x.log.append("undo a")),
        Step("b", lambda x: x.log.append("b"), compensate=lambda x: x.log.append("undo b")),
        Step("c", boom, retries=1),
        Step("d", lambda x: x.log.append("d")),
    ], sleep=lambda _: None)

    with pytest.raises(WorkflowError) as err:
        wf.run(c)
    assert err.value.step == "c"
    assert c.log == ["a", "b", "undo b", "undo a"]


def test_failing_compensation_does_not_stop_rollback():
    c = ctx()

    def bad_undo(_):
        raise RuntimeError("undo failed")

    def boom(_):
        raise RuntimeError("boom")

    wf = Workflow("t", [
        Step("a", lambda x: None, compensate=lambda x: x.log.append("undo a")),
        Step("b", lambda x: None, compensate=bad_undo),
        Step("c", boom),
    ])
    with pytest.raises(WorkflowError):
        wf.run(c)
    assert c.log == ["undo a"]
