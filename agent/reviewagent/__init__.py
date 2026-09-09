"""Minimal AI-PR-review agent used by the ikt-workshop security lab.

The package is deliberately split along the interfaces we expect to replace
later (see README "Future evolution"):

    pr.py        - repository / pull-request model
    skills.py    - skill discovery + representation
    llm/         - decision maker (FakeLLM today, RealLLM later)
    tools/       - tool execution surface
    kube/        - Kubernetes credentials + API access
    evidence.py  - structured forensic event log
    agent.py     - the loop that wires the above together
"""

__version__ = "0.1.0"
