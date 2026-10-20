"""Wall-clock deadlines for role calls: a late answer is discarded, never admitted.

Python cannot kill a running thread. The coordinator therefore stops waiting at the deadline
and decides fail-closed; adapters that perform I/O must also bound their own request with the
same timeout (the LLM transports do), so the abandoned call ends shortly after.
"""

from collections.abc import Callable
from threading import Event, Thread


class DeadlineExceeded(TimeoutError):
    """The role did not answer before its deadline; any later answer is ignored."""


def call_with_deadline[T](action: Callable[[], T], budget_ns: int) -> T:
    if budget_ns <= 0 or isinstance(budget_ns, bool):
        raise ValueError("Invalid deadline")
    done = Event()
    answer: list[T] = []
    failure: list[BaseException] = []

    def work() -> None:
        try:
            answer.append(action())
        except BaseException as error:  # Re-raised in the waiting thread.
            failure.append(error)
        finally:
            done.set()

    # Daemon: an abandoned call never keeps the experiment process alive.
    Thread(target=work, name="hnp-role-call", daemon=True).start()
    if not done.wait(budget_ns / 1e9):
        raise DeadlineExceeded("Role deadline exceeded")
    if failure:
        raise failure[0]
    return answer[0]
