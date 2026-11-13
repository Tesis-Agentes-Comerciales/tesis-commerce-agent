"""Agent process of the live stack. JSON lines on stdin/stdout; real HTTP never uses the pipe.

`AgentProcess` runs it as `python -m commerce_evaluation.stack.agent_worker`. The pipe only
provisions synthetic fixture secrets in memory and starts backend actions. Named cutpoints are
test-only injection through ports the Agent already accepts (the verifier's connection factory,
SqliteGuardedD1's transport): at a cutpoint the process announces it and parks until the harness
kills it. Production code is not patched.
"""
