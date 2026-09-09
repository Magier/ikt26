# ikt-workshop - phase 1
# Built by .github/workflows/agent-image.yml on the lab-infra branch and
# published to the workshop repository's GHCR. Override to build locally.
IMAGE ?= ghcr.io/magier/ikt26/review-agent:latest
NS    ?= ikt-workshop
# The repository under review: github.com/Magier/ikt26 pull request #1,
# cloned into .build/ by hack/fetch-pr-repo.sh.
PR_REPO   ?= .build/workshop-repo
PR_SLUG   ?= Magier/ikt26
PR_NUMBER ?= 1

.PHONY: help test repo repo-offline publish-repo publish-lab-infra image-push demo demo-mitigated inspect reset image kind-load cluster-deploy cluster-deploy-nobuild cluster-reset cluster-attack cluster-attack-mitigated cluster-evidence cluster-trace trace cluster-clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t24

test:  ## run the phase 1 test suite (no cluster needed)
	python3 -m unittest discover -s tests -v

repo:  ## clone the repository under review from github.com/$(PR_SLUG) PR #$(PR_NUMBER)
	WORKSHOP_SLUG=$(PR_SLUG) WORKSHOP_PR=$(PR_NUMBER) hack/fetch-pr-repo.sh

repo-offline:  ## rebuild the same history locally, no network (what the tests use)
	hack/build-pr-repo.sh

publish-repo:  ## instructor-only: force-push the history to GitHub and open the PR
	hack/publish-pr-repo.sh --yes

publish-lab-infra:  ## instructor-only: push this tree to the lab-infra branch (CI builds the image)
	hack/publish-lab-infra.sh --yes

demo: reset repo  ## run the whole scenario against the local simulator
	SIM_CONTAINER_TIMEOUT=3 WORKSHOP_REPO=$(PR_REPO) bin/review-pr

demo-mitigated: reset repo  ## same PR, but skills come from the base branch
	SIM_CONTAINER_TIMEOUT=3 WORKSHOP_REPO=$(PR_REPO) bin/review-pr --skills-ref main

inspect:  ## show the evidence the last local run produced
	bin/inspect-evidence

reset:  ## delete simulated cluster state and the audit log
	bin/reset

image: repo  ## build the agent image locally (the repo under review is baked in)
	docker build -f agent/Dockerfile -t $(IMAGE) .

image-push: image  ## push a locally built image (CI normally does this; needs write:packages)
	docker push $(IMAGE)

kind-load: image  ## load the image into a kind cluster named ikt-workshop
	kind load docker-image $(IMAGE) --name ikt-workshop

cluster-deploy:  ## apply namespace, target workload, RBAC and the agent (needs the image)
	kubectl apply -f deploy/00-namespace.yaml
	kubectl apply -f workshop-repo/deploy/application.yaml
	kubectl apply -f deploy/20-review-agent-rbac.yaml
	kubectl apply -f deploy/30-review-agent.yaml
	kubectl -n $(NS) rollout status deploy/ai-pr-review-agent

cluster-deploy-nobuild: repo  ## same, but no image: code from ConfigMaps, repo from a git bundle
	kubectl apply -f deploy/00-namespace.yaml
	kubectl apply -f workshop-repo/deploy/application.yaml
	kubectl apply -f deploy/20-review-agent-rbac.yaml
	python3 hack/gen-configmap-deploy.py | kubectl apply -f -
	kubectl -n $(NS) rollout restart deploy/ai-pr-review-agent
	kubectl -n $(NS) rollout status deploy/ai-pr-review-agent
	kubectl -n $(NS) rollout status deploy/payments-api

cluster-reset:  ## put the target workload back to its pristine state
	kubectl -n $(NS) delete deploy payments-api --ignore-not-found
	kubectl apply -f workshop-repo/deploy/application.yaml
	kubectl -n $(NS) rollout status deploy/payments-api

cluster-attack:  ## trigger the review inside the cluster (the attack)
	kubectl -n $(NS) exec deploy/ai-pr-review-agent -c agent -- \
	  python -m reviewagent.cli --backend cluster review-pr --quiet

cluster-attack-mitigated:  ## same review, skills loaded from the base branch
	kubectl -n $(NS) exec deploy/ai-pr-review-agent -c agent -- \
	  python -m reviewagent.cli --backend cluster --skills-ref main review-pr --quiet

cluster-evidence:  ## collect the cluster-side evidence
	@echo "== agent audit log"
	kubectl -n $(NS) exec deploy/ai-pr-review-agent -c agent -- cat /var/log/review-agent/events.jsonl
	@echo "\n== deployment rollout history"
	kubectl -n $(NS) rollout history deploy/payments-api
	@echo "\n== pods"
	kubectl -n $(NS) get pods -o wide
	@echo "\n== injected container logs"
	kubectl -n $(NS) logs deploy/payments-api -c runtime-config-sync --tail=30 || true
	@echo "\n== the marker, served by the application itself"
	kubectl -n $(NS) run marker-check --rm -i --restart=Never --image=curlimages/curl:8.11.1 --quiet -- \
	  -sS http://payments-api/agent-review-marker.txt || true
	@echo "\n== events"
	kubectl -n $(NS) get events --sort-by=.lastTimestamp | tail -25

cluster-trace:  ## export the agent trace + cluster evidence into audit/exports/cluster
	hack/export-trace.sh

trace:  ## export the trace of the last local run into audit/exports/local
	PYTHONPATH=agent python3 -m reviewagent.cli --repo $(PR_REPO) \
	  --audit-log audit/events.jsonl export-trace --out-dir audit/exports/local

cluster-clean:  ## remove the workshop namespace
	kubectl delete namespace $(NS) --ignore-not-found
