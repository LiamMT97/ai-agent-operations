# Code

Sanitised extracts from a personal agent system. They run as-is with the paths adjusted.

- `decide_chain.py` — decision layer with a five-level fallback chain, per-level circuit breaker
  and a local quota counter. CLI: `decide`, `estado`, `reset`.
- `command_guard.py` — pre-execution guard that blocks catastrophic shell commands before any
  agent runs them. Fails open on its own errors on purpose.
- `dangerous-patterns.txt` — the shared pattern list (52 active patterns, POSIX ERE). Originally
  from the global-agent-guardrails skill (davidondrej/skills), adapted.

No credentials are stored in this repository: keys are read from files with restricted
permissions outside the config, or from the environment.
