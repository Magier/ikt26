# Local simulator state

`seed/workshop-app.json` mirrors `workshop-repo/deploy/application.yaml` as
JSON so the local backend can load the cluster's starting state without a YAML
dependency in the agent. Keep the two in sync when you change the manifest -
`tests/test_phase1.py::test_seed_matches_manifest` checks the container names,
images and volume names.

`state/` (git-ignored) holds a run's simulated objects, pod logs and container
sandboxes. Delete it to reset the "cluster".
