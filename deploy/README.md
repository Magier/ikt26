# Cluster manifests

Apply in this order:

```sh
kubectl apply -f deploy/00-namespace.yaml
kubectl apply -f workshop-repo/deploy/application.yaml   # the target workload
kubectl apply -f deploy/20-review-agent-rbac.yaml
kubectl apply -f deploy/30-review-agent.yaml
```

The target workload is applied straight from `workshop-repo/deploy/` on
purpose: in the story, that file *is* the deployment source of truth, and the
attack works by making the live object diverge from it. Keeping one copy means
the drift participants find is real.

`kind/` holds an optional kind cluster config that enables API server
auditing, so the Kubernetes-side evidence is available too.
