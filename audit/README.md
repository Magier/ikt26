# Evidence output

`events.jsonl` is the agent's audit log for local runs (git-ignored).

`exports/<where>/<run-id>/` holds rendered traces, written by
`make trace` (local) or `make cluster-trace` (from a cluster):

| file | what it is |
|---|---|
| `events.jsonl` | just that run's structured events |
| `trace.md` | annotated timeline, including the trust-boundary crossing |
| `commands.sh` | the same API calls as plain kubectl commands |
| `patch-NN.json` | the bodies those commands need |

`exports/cluster/cluster/` additionally holds the cluster-side snapshot:
live objects, Events, rollout history, RBAC, injected container logs and an
authoritative `SubjectAccessReview` table.

Exports are git-ignored: they are run artefacts, not source.
