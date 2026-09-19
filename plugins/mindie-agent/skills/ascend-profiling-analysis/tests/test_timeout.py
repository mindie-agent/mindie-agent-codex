"""Remote job timeout and missing job-reference failures at the analysis skill layer."""
from __future__ import annotations

from unittest import mock

import conftest  # noqa: F401

import _common as common


def _local_endpoint() -> common.SshEndpoint:
    return common.SshEndpoint(host="192.0.2.10", port=22, user="root")


def test_remote_job_requires_positive_timeout() -> None:
    try:
        common.run_remote_job(_local_endpoint(), "true", timeout=0, name="t")
    except RuntimeError as exc:
        assert "timeout" in str(exc).lower()
    else:
        raise AssertionError("timeout=0 must fail closed")


def test_timed_out_job_raises_timeout_error() -> None:
    with mock.patch.object(common._mindie_exec, "start_job", return_value="job-1"), mock.patch.object(
        common._mindie_exec, "job_status", return_value={"outcome": "success", "state": "running"}
    ), mock.patch.object(common._mindie_exec, "job_stop") as stop, mock.patch.object(
        common.time, "monotonic", side_effect=[0.0, 5.0]
    ):
        try:
            common.run_remote_job(_local_endpoint(), "sleep 30", timeout=2, name="t")
        except TimeoutError as exc:
            assert "job-1" in str(exc)
            assert "2" in str(exc)
        else:
            raise AssertionError("timed-out job must raise TimeoutError")
        stop.assert_called()


def test_completed_job_returns_exit_code() -> None:
    with mock.patch.object(common._mindie_exec, "start_job", return_value="job-ok"), mock.patch.object(
        common._mindie_exec, "job_status", return_value={"outcome": "success", "state": "completed", "exit_code": 0}
    ):
        rc, job_id = common.run_remote_job(_local_endpoint(), "true", timeout=10, name="t")
    assert rc == 0
    assert job_id == "job-ok"


def test_missing_job_id_is_explicit_failure() -> None:
    with mock.patch.object(common._mindie_exec, "start_job", return_value=""):
        try:
            common.run_remote_job(_local_endpoint(), "true", timeout=10, name="t")
        except RuntimeError as exc:
            assert "execution reference" in str(exc)
        else:
            raise AssertionError("empty job id must fail")


if __name__ == "__main__":
    test_remote_job_requires_positive_timeout()
    test_timed_out_job_raises_timeout_error()
    test_completed_job_returns_exit_code()
    test_missing_job_id_is_explicit_failure()
