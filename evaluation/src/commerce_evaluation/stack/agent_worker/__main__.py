"""The Agent worker's loop: one JSON action per line, one JSON answer."""

import json
import os
import sys
from typing import Any

from commerce_evaluation.stack.agent_worker.guarded import Guarded
from commerce_evaluation.stack.agent_worker.runtime import confine, failure
from commerce_evaluation.stack.agent_worker.verifier import Verifier


def main() -> None:
    sys.addaudithook(confine)
    case: Verifier | Guarded | None = None
    for line in sys.stdin:
        value = json.loads(line)
        action = value["action"]
        if action == "close":
            break
        try:
            if action == "verifier":
                case = Verifier(value)
                result: Any = {"pid": os.getpid()}
            elif action == "guarded":
                case = Guarded(value["data"], fresh=value["fresh"])
                result = case.opened(value)
            else:
                operation = getattr(case, action, None)
                if action.startswith("_") or not callable(operation):
                    raise RuntimeError("LIVE_AGENT_ACTION_REJECTED")
                result = operation(value)
            print(json.dumps({"ok": True, "result": result}), flush=True)
        except Exception as error:
            diagnostic = failure(error)
            print(json.dumps(diagnostic), file=sys.stderr, flush=True)
            print(json.dumps(diagnostic), flush=True)
    if case is not None:
        case.close()


if __name__ == "__main__":
    main()
