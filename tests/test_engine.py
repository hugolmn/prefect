import time
from contextlib import contextmanager
from functools import partial
from unittest.mock import MagicMock

import anyio
import pendulum
import pytest

from prefect import engine, flow, task
from prefect.context import FlowRunContext
from prefect.engine import (
    begin_flow_run,
    orchestrate_flow_run,
    orchestrate_task_run,
    retrieve_flow_then_begin_flow_run,
)
from prefect.exceptions import ParameterTypeError
from prefect.futures import PrefectFuture
from prefect.orion.schemas.filters import FlowRunFilter
from prefect.orion.schemas.states import (
    Cancelled,
    Failed,
    Pending,
    Running,
    State,
    StateDetails,
    StateType,
)
from prefect.task_runners import SequentialTaskRunner
from prefect.testing.utilities import AsyncMock, exceptions_equal, flaky_on_windows
from prefect.utilities.collections import quote
from prefect.utilities.pydantic import PartialModel


@pytest.fixture
def mock_client_sleep(monkeypatch):
    """
    Mock sleep used by the orion_client to not actually sleep but to set the
    current time to now + sleep delay seconds.
    """
    original_now = pendulum.now
    time_shift = 0

    async def callback(delay_in_seconds):
        nonlocal time_shift
        time_shift += delay_in_seconds

    monkeypatch.setattr(
        "pendulum.now", lambda *args: original_now(*args).add(seconds=time_shift)
    )

    sleep = AsyncMock(side_effect=callback)
    monkeypatch.setattr("prefect.client.sleep", sleep)

    return sleep


class TestOrchestrateTaskRun:
    async def test_waits_until_scheduled_start_time(
        self,
        orion_client,
        flow_run,
        mock_client_sleep,
        local_filesystem,
        monkeypatch,
    ):
        @task
        def foo():
            return 1

        task_run = await orion_client.create_task_run(
            task=foo,
            flow_run_id=flow_run.id,
            dynamic_key="0",
            state=State(
                type=StateType.SCHEDULED,
                state_details=StateDetails(
                    scheduled_time=pendulum.now("utc").add(minutes=5)
                ),
            ),
        )

        state = await orchestrate_task_run(
            task=foo,
            task_run=task_run,
            parameters={},
            wait_for=None,
            result_filesystem=local_filesystem,
            interruptible=False,
            client=orion_client,
        )

        mock_client_sleep.assert_awaited_once()
        assert state.is_completed()
        assert state.result() == 1

    async def test_does_not_wait_for_scheduled_time_in_past(
        self, orion_client, flow_run, mock_client_sleep, local_filesystem
    ):
        @task
        def foo():
            return 1

        task_run = await orion_client.create_task_run(
            task=foo,
            flow_run_id=flow_run.id,
            dynamic_key="0",
            state=State(
                type=StateType.SCHEDULED,
                state_details=StateDetails(
                    scheduled_time=pendulum.now("utc").subtract(minutes=5)
                ),
            ),
        )

        state = await orchestrate_task_run(
            task=foo,
            task_run=task_run,
            parameters={},
            wait_for=None,
            result_filesystem=local_filesystem,
            interruptible=False,
            client=orion_client,
        )

        mock_client_sleep.assert_not_called()
        assert state.is_completed()
        assert state.result() == 1

    async def test_waits_for_awaiting_retry_scheduled_time(
        self, mock_client_sleep, orion_client, flow_run, local_filesystem
    ):
        # Define a task that fails once and then succeeds
        mock = MagicMock()

        @task(retries=1, retry_delay_seconds=43)
        def flaky_function():
            mock()

            if mock.call_count == 2:
                return 1

            raise ValueError("try again, but only once")

        # Create a task run to test
        task_run = await orion_client.create_task_run(
            task=flaky_function,
            flow_run_id=flow_run.id,
            state=Pending(),
            dynamic_key="0",
        )

        # Actually run the task
        state = await orchestrate_task_run(
            task=flaky_function,
            task_run=task_run,
            parameters={},
            wait_for=None,
            result_filesystem=local_filesystem,
            interruptible=False,
            client=orion_client,
        )

        # Check for a proper final result
        assert state.result() == 1

        # Assert that the sleep was called
        # due to network time and rounding, the expected sleep time will be less than
        # 43 seconds so we test a window
        mock_client_sleep.assert_awaited_once()
        assert 40 < mock_client_sleep.call_args[0][0] < 43

        # Check expected state transitions
        states = await orion_client.read_task_run_states(task_run.id)
        state_names = [state.type for state in states]
        assert state_names == [
            StateType.PENDING,
            StateType.RUNNING,
            StateType.SCHEDULED,
            StateType.RUNNING,
            StateType.COMPLETED,
        ]

    @pytest.mark.parametrize(
        "upstream_task_state", [Pending(), Running(), Cancelled(), Failed()]
    )
    async def test_returns_not_ready_when_any_upstream_futures_resolve_to_incomplete(
        self, orion_client, flow_run, upstream_task_state, local_filesystem
    ):
        # Define a mock to ensure the task was not run
        mock = MagicMock()

        @task
        def my_task(x):
            mock()

        # Create an upstream task run
        upstream_task_run = await orion_client.create_task_run(
            task=my_task,
            flow_run_id=flow_run.id,
            state=upstream_task_state,
            dynamic_key="upstream",
        )
        upstream_task_state.state_details.task_run_id = upstream_task_run.id

        # Create a future to wrap the upstream task, have it resolve to the given
        # incomplete state
        future = PrefectFuture(
            task_run=upstream_task_run,
            run_key=str(upstream_task_run.id),
            task_runner=None,
            _final_state=upstream_task_state,
        )

        # Create a task run to test
        task_run = await orion_client.create_task_run(
            task=my_task,
            flow_run_id=flow_run.id,
            state=Pending(),
            dynamic_key="downstream",
        )

        # Actually run the task
        state = await orchestrate_task_run(
            task=my_task,
            task_run=task_run,
            # Nest the future in a collection to ensure that it is found
            parameters={"x": {"nested": [future]}},
            wait_for=None,
            result_filesystem=local_filesystem,
            interruptible=False,
            client=orion_client,
        )

        # The task did not run
        mock.assert_not_called()

        # Check that the state is 'NotReady'
        assert state.is_pending()
        assert state.name == "NotReady"
        assert (
            state.message
            == f"Upstream task run '{upstream_task_run.id}' did not reach a 'COMPLETED' state."
        )

    async def test_quoted_parameters_are_resolved(
        self, orion_client, flow_run, local_filesystem
    ):
        # Define a mock to ensure the task was not run
        mock = MagicMock()

        @task
        def my_task(x):
            mock(x)

        # Create a task run to test
        task_run = await orion_client.create_task_run(
            task=my_task,
            flow_run_id=flow_run.id,
            state=Pending(),
            dynamic_key="downstream",
        )

        # Actually run the task
        state = await orchestrate_task_run(
            task=my_task,
            task_run=task_run,
            # Quote some data
            parameters={"x": quote(1)},
            wait_for=None,
            result_filesystem=local_filesystem,
            interruptible=False,
            client=orion_client,
        )

        # The task ran with the unqoted data
        mock.assert_called_once_with(1)

        # Check that the state completed happily
        assert state.is_completed()

    @pytest.mark.parametrize(
        "upstream_task_state", [Pending(), Running(), Cancelled(), Failed()]
    )
    async def test_states_in_parameters_can_be_incomplete_if_quoted(
        self, orion_client, flow_run, upstream_task_state, local_filesystem
    ):
        # Define a mock to ensure the task was not run
        mock = MagicMock()

        @task
        def my_task(x):
            mock(x)

        # Create a task run to test
        task_run = await orion_client.create_task_run(
            task=my_task,
            flow_run_id=flow_run.id,
            state=Pending(),
            dynamic_key="downstream",
        )

        # Actually run the task
        state = await orchestrate_task_run(
            task=my_task,
            task_run=task_run,
            parameters={"x": quote(upstream_task_state)},
            wait_for=None,
            result_filesystem=local_filesystem,
            interruptible=False,
            client=orion_client,
        )

        # The task ran with the state as its input
        mock.assert_called_once_with(upstream_task_state)

        # Check that the task completed happily
        assert state.is_completed()

    @flaky_on_windows
    async def test_interrupt_task(self):
        i = 0

        @task()
        def just_sleep():
            nonlocal i
            for i in range(100):  # Sleep for 10 seconds
                time.sleep(0.1)

        @flow
        def my_flow():
            with pytest.raises(TimeoutError):
                with anyio.fail_after(1):
                    just_sleep()

        t0 = time.perf_counter()
        my_flow._run()
        t1 = time.perf_counter()

        runtime = t1 - t0
        assert runtime < 2, "The call should be return quickly after timeout"

        # Sleep for an extra second to check if the thread is still running. We cannot
        # check `thread.is_alive()` because it is still alive — presumably this is because
        # AnyIO is using long-lived worker threads instead of creating a new thread per
        # task. Without a check like this, the thread can be running after timeout in the
        # background and we will not know — the next test will start.
        await anyio.sleep(1)

        assert i <= 10, "`just_sleep` should not be running after timeout"


class TestOrchestrateFlowRun:
    @pytest.fixture
    def partial_flow_run_context(self, local_filesystem):
        return PartialModel(
            FlowRunContext,
            task_runner=SequentialTaskRunner(),
            sync_portal=None,
            result_filesystem=local_filesystem,
        )

    async def test_waits_until_scheduled_start_time(
        self, orion_client, mock_client_sleep, partial_flow_run_context
    ):
        @flow
        def foo():
            return 1

        flow_run = await orion_client.create_flow_run(
            flow=foo,
            state=State(
                type=StateType.SCHEDULED,
                state_details=StateDetails(
                    scheduled_time=pendulum.now("utc").add(minutes=5)
                ),
            ),
        )

        state = await orchestrate_flow_run(
            flow=foo,
            flow_run=flow_run,
            parameters={},
            client=orion_client,
            interruptible=False,
            partial_flow_run_context=partial_flow_run_context,
        )

        mock_client_sleep.assert_awaited_once()
        assert state.result() == 1

    async def test_does_not_wait_for_scheduled_time_in_past(
        self, orion_client, mock_client_sleep, partial_flow_run_context
    ):
        @flow
        def foo():
            return 1

        flow_run = await orion_client.create_flow_run(
            flow=foo,
            state=State(
                type=StateType.SCHEDULED,
                state_details=StateDetails(
                    scheduled_time=pendulum.now("utc").subtract(minutes=5)
                ),
            ),
        )

        with anyio.fail_after(5):
            state = await orchestrate_flow_run(
                flow=foo,
                flow_run=flow_run,
                parameters={},
                client=orion_client,
                interruptible=False,
                partial_flow_run_context=partial_flow_run_context,
            )

        mock_client_sleep.assert_not_called()
        assert state.result() == 1

    async def test_waits_for_awaiting_retry_scheduled_time(
        self, orion_client, mock_client_sleep, partial_flow_run_context
    ):
        flow_run_count = 0

        @flow(retries=1, retry_delay_seconds=43)
        def flaky_function():
            nonlocal flow_run_count
            flow_run_count += 1

            if flow_run_count == 1:
                raise ValueError("try again, but only once")

            return 1

        flow_run = await orion_client.create_flow_run(
            flow=flaky_function, state=Pending()
        )

        state = await orchestrate_flow_run(
            flow=flaky_function,
            flow_run=flow_run,
            parameters={},
            client=orion_client,
            interruptible=False,
            partial_flow_run_context=partial_flow_run_context,
        )

        # Check for a proper final result
        assert state.result() == 1

        # Assert that the sleep was called
        # due to network time and rounding, the expected sleep time will be less than
        # 43 seconds so we test a window
        mock_client_sleep.assert_awaited_once()
        assert 40 < mock_client_sleep.call_args[0][0] < 43

        # Check expected state transitions
        states = await orion_client.read_flow_run_states(flow_run.id)
        state_names = [state.type for state in states]
        assert state_names == [
            StateType.PENDING,
            StateType.RUNNING,
            StateType.SCHEDULED,
            StateType.RUNNING,
            StateType.COMPLETED,
        ]


class TestFlowRunCrashes:
    @staticmethod
    @contextmanager
    def capture_cancellation():
        """Utility for capturing crash exceptions consistently in these tests"""
        try:
            yield
        except BaseException:
            # In python 3.8+ cancellation raises a `BaseException` that will not
            # be captured by `orchestrate_flow_run` and needs to be trapped here to
            # prevent the test from failing before we can assert things are 'Crashed'
            pass
        except anyio.get_cancelled_exc_class() as exc:
            raise RuntimeError("The cancellation error was not caught.") from exc

    async def test_anyio_cancellation_crashes_flow(self, flow_run, orion_client):
        started = anyio.Event()

        @flow
        async def my_flow():
            started.set()
            await anyio.sleep_forever()

        with self.capture_cancellation():
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    partial(
                        begin_flow_run,
                        flow=my_flow,
                        flow_run=flow_run,
                        parameters={},
                        client=orion_client,
                    )
                )
                await started.wait()
                tg.cancel_scope.cancel()

        flow_run = await orion_client.read_flow_run(flow_run.id)

        assert flow_run.state.is_crashed()
        assert flow_run.state.type == StateType.CRASHED
        assert (
            "Execution was cancelled by the runtime environment"
            in flow_run.state.message
        )
        assert exceptions_equal(
            flow_run.state.result(raise_on_failure=False),
            anyio.get_cancelled_exc_class()(),
        )

    async def test_anyio_cancellation_crashes_subflow(self, flow_run, orion_client):
        started = anyio.Event()

        @flow
        async def child_flow():
            started.set()
            await anyio.sleep_forever()

        @flow
        async def parent_flow():
            await child_flow()

        with self.capture_cancellation():
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    partial(
                        begin_flow_run,
                        flow=parent_flow,
                        parameters={},
                        flow_run=flow_run,
                        client=orion_client,
                    )
                )
                await started.wait()
                tg.cancel_scope.cancel()

        parent_flow_run = await orion_client.read_flow_run(flow_run.id)
        assert parent_flow_run.state.is_crashed()
        assert parent_flow_run.state.type == StateType.CRASHED
        assert exceptions_equal(
            parent_flow_run.state.result(raise_on_failure=False),
            anyio.get_cancelled_exc_class()(),
        )

        child_runs = await orion_client.read_flow_runs(
            flow_run_filter=FlowRunFilter(parent_task_run_id=dict(is_null_=False))
        )
        assert len(child_runs) == 1
        child_run = child_runs[0]
        assert child_run.state.is_crashed()
        assert child_run.state.type == StateType.CRASHED
        assert (
            "Execution was cancelled by the runtime environment"
            in child_run.state.message
        )

    @pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
    async def test_interrupt_in_flow_function_crashes_flow(
        self, flow_run, orion_client, interrupt_type
    ):
        @flow
        async def my_flow():
            raise interrupt_type()

        with pytest.raises(interrupt_type):
            await begin_flow_run(
                flow=my_flow, flow_run=flow_run, parameters={}, client=orion_client
            )

        flow_run = await orion_client.read_flow_run(flow_run.id)
        assert flow_run.state.is_crashed()
        assert flow_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in flow_run.state.message
        assert exceptions_equal(
            flow_run.state.result(raise_on_failure=False), interrupt_type()
        )

    @pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
    async def test_interrupt_during_orchestration_crashes_flow(
        self, flow_run, orion_client, monkeypatch, interrupt_type
    ):
        monkeypatch.setattr(
            "prefect.client.OrionClient.propose_state",
            MagicMock(side_effect=interrupt_type()),
        )

        @flow
        async def my_flow():
            pass

        with pytest.raises(interrupt_type):
            await begin_flow_run(
                flow=my_flow, flow_run=flow_run, parameters={}, client=orion_client
            )

        flow_run = await orion_client.read_flow_run(flow_run.id)
        assert flow_run.state.is_crashed()
        assert flow_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in flow_run.state.message
        with pytest.warns(UserWarning, match="not safe to re-raise"):
            assert exceptions_equal(flow_run.state.result(), interrupt_type())

    @pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
    async def test_interrupt_in_flow_function_crashes_subflow(
        self, flow_run, orion_client, interrupt_type
    ):
        @flow
        async def child_flow():
            raise interrupt_type()

        @flow
        async def parent_flow():
            await child_flow()

        with pytest.raises(interrupt_type):
            await begin_flow_run(
                flow=parent_flow, flow_run=flow_run, parameters={}, client=orion_client
            )

        flow_run = await orion_client.read_flow_run(flow_run.id)
        assert flow_run.state.is_crashed()
        assert flow_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in flow_run.state.message
        assert exceptions_equal(
            flow_run.state.result(raise_on_failure=False), interrupt_type()
        )

        child_runs = await orion_client.read_flow_runs(
            flow_run_filter=FlowRunFilter(parent_task_run_id=dict(is_null_=False))
        )
        assert len(child_runs) == 1
        child_run = child_runs[0]
        assert child_run.id != flow_run.id
        assert child_run.state.is_crashed()
        assert child_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in child_run.state.message

    async def test_flow_timeouts_are_not_crashes(self, flow_run, orion_client):
        """
        Since timeouts use anyio cancellation scopes, we want to ensure that they are
        not marked as crashes
        """

        @flow(timeout_seconds=0.1)
        async def my_flow():
            await anyio.sleep_forever()

        await begin_flow_run(
            flow=my_flow,
            parameters={},
            flow_run=flow_run,
            client=orion_client,
        )
        flow_run = await orion_client.read_flow_run(flow_run.id)

        assert flow_run.state.is_failed()
        assert flow_run.state.type != StateType.CRASHED
        assert "exceeded timeout" in flow_run.state.message

    async def test_timeouts_do_not_hide_crashes(self, flow_run, orion_client):
        """
        Since timeouts capture anyio cancellations, we want to ensure that something
        still ends up in a 'Crashed' state if it is cancelled independently from our
        timeout cancellation.
        """
        started = anyio.Event()

        @flow(timeout_seconds=100)
        async def my_flow():
            started.set()
            await anyio.sleep_forever()

        with self.capture_cancellation():
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    partial(
                        begin_flow_run,
                        parameters={},
                        flow=my_flow,
                        flow_run=flow_run,
                        client=orion_client,
                    )
                )
                await started.wait()
                tg.cancel_scope.cancel()

        flow_run = await orion_client.read_flow_run(flow_run.id)

        assert flow_run.state.is_crashed()
        assert flow_run.state.type == StateType.CRASHED
        assert (
            "Execution was cancelled by the runtime environment"
            in flow_run.state.message
        )

    async def test_interrupt_flow(self):
        i = 0

        @flow()
        def just_sleep():
            nonlocal i
            for i in range(100):  # Sleep for 10 seconds
                time.sleep(0.1)

        @flow
        def my_flow():
            with pytest.raises(TimeoutError):
                with anyio.fail_after(1):
                    just_sleep()

        t0 = time.perf_counter()
        my_flow._run()
        t1 = time.perf_counter()

        runtime = t1 - t0
        assert runtime < 2, "The call should be return quickly after timeout"

        # Sleep for an extra second to check if the thread is still running. We cannot
        # check `thread.is_alive()` because it is still alive — presumably this is because
        # AnyIO is using long-lived worker threads instead of creating a new thread per
        # task. Without a check like this, the thread can be running after timeout in the
        # background and we will not know — the next test will start.
        await anyio.sleep(1)

        assert i <= 10, "`just_sleep` should not be running after timeout"


class TestTaskRunCrashes:
    @pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
    async def test_interrupt_in_task_function_crashes_task_and_flow(
        self, flow_run, orion_client, interrupt_type
    ):
        @task
        async def my_task():
            raise interrupt_type()

        @flow
        async def my_flow():
            await my_task()

        with pytest.raises(interrupt_type):
            await begin_flow_run(
                flow=my_flow, flow_run=flow_run, parameters={}, client=orion_client
            )

        flow_run = await orion_client.read_flow_run(flow_run.id)
        assert flow_run.state.is_crashed()
        assert flow_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in flow_run.state.message
        with pytest.warns(UserWarning, match="not safe to re-raise"):
            assert exceptions_equal(flow_run.state.result(), interrupt_type())

        task_runs = await orion_client.read_task_runs()
        assert len(task_runs) == 1
        task_run = task_runs[0]
        assert task_run.state.is_crashed()
        assert task_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in task_run.state.message
        with pytest.warns(UserWarning, match="not safe to re-raise"):
            assert exceptions_equal(task_run.state.result(), interrupt_type())

    @pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
    async def test_interrupt_in_task_orchestration_crashes_task_and_flow(
        self, flow_run, orion_client, interrupt_type, monkeypatch
    ):

        monkeypatch.setattr(
            "prefect.engine.orchestrate_task_run", AsyncMock(side_effect=interrupt_type)
        )

        @task
        async def my_task():
            pass

        @flow
        async def my_flow():
            await my_task()

        with pytest.raises(interrupt_type):
            await begin_flow_run(
                flow=my_flow, flow_run=flow_run, parameters={}, client=orion_client
            )

        flow_run = await orion_client.read_flow_run(flow_run.id)
        assert flow_run.state.is_crashed()
        assert flow_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in flow_run.state.message
        with pytest.warns(UserWarning, match="not safe to re-raise"):
            assert exceptions_equal(flow_run.state.result(), interrupt_type())

        task_runs = await orion_client.read_task_runs()
        assert len(task_runs) == 1
        task_run = task_runs[0]
        assert task_run.state.is_crashed()
        assert task_run.state.type == StateType.CRASHED
        assert "Execution was aborted" in task_run.state.message
        with pytest.warns(UserWarning, match="not safe to re-raise"):
            assert exceptions_equal(task_run.state.result(), interrupt_type())

    async def test_error_in_task_orchestration_crashes_task_but_not_flow(
        self, flow_run, orion_client, monkeypatch
    ):
        exception = ValueError("Boo!")

        monkeypatch.setattr(
            "prefect.engine.orchestrate_task_run", AsyncMock(side_effect=exception)
        )

        @task
        async def my_task():
            pass

        @flow
        async def my_flow():
            await my_task._run()

        # Note exception should not be re-raised
        state = await begin_flow_run(
            flow=my_flow, flow_run=flow_run, parameters={}, client=orion_client
        )

        flow_run = await orion_client.read_flow_run(flow_run.id)
        assert flow_run.state.is_failed()
        assert flow_run.state.name == "Failed"
        assert "1/1 states failed" in flow_run.state.message

        task_run_states = state.result(raise_on_failure=False)
        assert len(task_run_states) == 1
        task_run_state = task_run_states[0]
        assert task_run_state.is_crashed()
        assert task_run_state.type == StateType.CRASHED
        assert (
            "Execution was interrupted by an unexpected exception"
            in task_run_state.message
        )
        assert exceptions_equal(
            task_run_state.result(raise_on_failure=False), exception
        )

        # Check that the state was reported to the server
        task_run = await orion_client.read_task_run(
            task_run_state.state_details.task_run_id
        )
        compare_fields = {"name", "type", "message"}
        assert task_run_state.dict(include=compare_fields) == task_run.state.dict(
            include=compare_fields
        )


class TestDeploymentFlowRun:
    @pytest.fixture
    async def patch_manifest_load(self, monkeypatch):
        async def patch_manifest(f):
            async def anon(*args, **kwargs):
                return f

            monkeypatch.setattr(
                engine,
                "load_flow_from_flow_run",
                anon,
            )
            return f

        return patch_manifest

    async def create_deployment(self, client, flow):
        flow_id = await client.create_flow(flow)
        return await client.create_deployment(
            flow_id,
            name="test",
            manifest_path="file.json",
        )

    async def test_completed_run(self, orion_client, patch_manifest_load):
        @flow
        def my_flow(x: int):
            return x

        await patch_manifest_load(my_flow)
        deployment_id = await self.create_deployment(orion_client, my_flow)

        flow_run = await orion_client.create_flow_run_from_deployment(
            deployment_id, parameters={"x": 1}
        )

        state = await retrieve_flow_then_begin_flow_run(
            flow_run.id, client=orion_client
        )
        assert state.result() == 1

    async def test_failed_run(self, orion_client, patch_manifest_load):
        @flow
        def my_flow(x: int):
            raise ValueError("test!")

        await patch_manifest_load(my_flow)
        deployment_id = await self.create_deployment(orion_client, my_flow)

        flow_run = await orion_client.create_flow_run_from_deployment(
            deployment_id, parameters={"x": 1}
        )

        state = await retrieve_flow_then_begin_flow_run(
            flow_run.id, client=orion_client
        )
        assert state.is_failed()
        with pytest.raises(ValueError, match="test!"):
            state.result()

    async def test_parameters_are_cast_to_correct_type(
        self, orion_client, patch_manifest_load
    ):
        @flow
        def my_flow(x: int):
            return x

        await patch_manifest_load(my_flow)
        deployment_id = await self.create_deployment(orion_client, my_flow)

        flow_run = await orion_client.create_flow_run_from_deployment(
            deployment_id, parameters={"x": "1"}
        )

        state = await retrieve_flow_then_begin_flow_run(
            flow_run.id, client=orion_client
        )
        assert state.result() == 1

    async def test_state_is_failed_when_parameters_fail_validation(
        self, orion_client, patch_manifest_load
    ):
        @flow
        def my_flow(x: int):
            return x

        await patch_manifest_load(my_flow)
        deployment_id = await self.create_deployment(orion_client, my_flow)

        flow_run = await orion_client.create_flow_run_from_deployment(
            deployment_id, parameters={"x": "not-an-int"}
        )

        state = await retrieve_flow_then_begin_flow_run(
            flow_run.id, client=orion_client
        )
        assert state.is_failed()
        assert state.message == "Flow run received invalid parameters."
        with pytest.raises(ParameterTypeError, match="value is not a valid integer"):
            state.result()


class TestDynamicKeyHandling:
    async def test_dynamic_key_increases_sequentially(self, orion_client):
        @task
        def my_task():
            pass

        @flow
        def my_flow():
            my_task()
            my_task()
            my_task()

        my_flow()

        task_runs = await orion_client.read_task_runs()

        assert sorted([int(run.dynamic_key) for run in task_runs]) == [0, 1, 2]

    async def test_subflow_resets_dynamic_key(self, orion_client):
        @task
        def my_task():
            pass

        @flow
        def subflow():
            my_task()

        @flow
        def my_flow():
            my_task()
            my_task()
            subflow()
            my_task()

        state = my_flow._run()

        task_runs = await orion_client.read_task_runs()
        parent_task_runs = [
            task_run
            for task_run in task_runs
            if task_run.flow_run_id == state.state_details.flow_run_id
        ]
        subflow_task_runs = [
            task_run
            for task_run in task_runs
            if task_run.flow_run_id != state.state_details.flow_run_id
        ]

        assert len(parent_task_runs) == 4  # 3 standard task runs and 1 subflow
        assert len(subflow_task_runs) == 1

        assert int(subflow_task_runs[0].dynamic_key) == 0

    async def test_dynamic_key_unique_per_task_key(self, orion_client):
        @task
        def task_one():
            pass

        @task
        def task_two():
            pass

        @flow
        def my_flow():
            task_one()
            task_two()
            task_two()
            task_one()

        my_flow()

        task_runs = await orion_client.read_task_runs()

        assert sorted([int(run.dynamic_key) for run in task_runs]) == [0, 0, 1, 1]
