import pickle

import pytest

from miniflow import Task, TaskDefinitionError, task


@task
def plain(x):
    return x + 1


@task(kind="cpu", retries=2, timeout=5, args=(3,))
def square(n):
    return n * n


@task(kind="async", after=[square, "plain"])
async def report(a, b):
    return f"{a}/{b}"


def test_bare_decorator_defaults():
    assert isinstance(plain, Task)
    assert plain.name == "plain"
    assert plain.kind == "io"
    assert plain.after == ()
    assert plain.max_attempts == 1


def test_options_are_recorded():
    assert square.kind == "cpu"
    assert square.retries == 2
    assert square.max_attempts == 3
    assert square.timeout == 5
    assert square.args == (3,)
    assert report.after == ("square", "plain")


def test_task_is_still_callable():
    assert plain(1) == 2
    assert square(4) == 16


def test_cpu_task_pickles_by_reference():
    clone = pickle.loads(pickle.dumps(square))
    assert clone is square


def test_custom_name():
    @task(name="renamed")
    def f():
        pass

    assert f.name == "renamed"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"kind": "gpu"}, "kind must be one of"),
        ({"retries": -1}, "retries"),
        ({"timeout": 0}, "timeout"),
        ({"after": ["f"]}, "depends on itself"),
        ({"after": ["a", "a"]}, "twice"),
        ({"after": [42]}, "dependencies must be"),
    ],
)
def test_invalid_options(options, message):
    with pytest.raises(TaskDefinitionError, match=message):

        @task(**options)
        def f():
            pass


def test_async_kind_requires_coroutine():
    with pytest.raises(TaskDefinitionError, match="async def"):

        @task(kind="async")
        def f():
            pass


def test_coroutine_requires_async_kind():
    with pytest.raises(TaskDefinitionError, match="kind='async'"):

        @task(kind="io")
        async def f():
            pass


def test_cpu_task_rejects_local_function():
    with pytest.raises(TaskDefinitionError, match="module-level"):

        @task(kind="cpu")
        def local():
            pass


def test_cpu_task_rejects_lambda():
    with pytest.raises(TaskDefinitionError, match="module-level"):
        task(lambda: 1, kind="cpu")
