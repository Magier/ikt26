# The review agent, on netshoot.
#
# Base: nicolaka/netshoot. An unusual choice for a service image, and a
# deliberate one - this container is the workshop's crime scene. Participants
# `kubectl exec` into it to see what the agent's identity can reach, and that
# investigation needs curl, nmap, tcpdump, tshark, socat, dig and jq already
# present. A scratch image would force every question through a tool we had to
# smuggle in, which is exactly the evidence-handling mistake the lab teaches.
#
# It is also the honest picture of the risk: a review agent whose pod is this
# well equipped is a lateral-movement platform the moment its skill file lies
# to it. `nicolaka/netshoot` was already what deploy/30-review-agent.yaml ran
# (sleeping); this image makes that container actually serve the agent.
#
# Pinned by digest, not by tag. netshoot builds from Alpine *edge*, so the
# same tag resolves to a different package set week to week - which would make
# "which python is in the image" unanswerable at the exact moment a
# participant asks it.
FROM nicolaka/netshoot:v0.16@sha256:b09d9b21381f47a79b3cbcb30da25266dc17186ea00ae65e99fdc51396f48e70

# Links the GHCR package to the repository, so it lands on the repo's Packages
# page instead of floating loose in the user namespace.
LABEL org.opencontainers.image.source="https://github.com/Magier/ikt26" \
      org.opencontainers.image.title="ai-pr-review-agent" \
      org.opencontainers.image.description="Workshop PR review agent (stdlib only) on a netshoot base" \
      org.opencontainers.image.licenses="Apache-2.0"

# No apk install step, on purpose. python3 and git both ship in the base, and
# apk here would resolve against Alpine edge at build time - re-introducing the
# drift the digest pin just removed. Assert instead of install: if a future
# netshoot drops either one, the build fails here with a clear reason rather
# than at the first `kubectl exec` in front of a room.
RUN python3 --version && git --version

WORKDIR /app
COPY agent/reviewagent/ /app/reviewagent/

# The repository under review - github.com/Magier/ikt26 pull request #1, with
# its real git history. Generated, not cloned:
#
#   hack/build-pr-repo.sh
#   docker build -f agent/Dockerfile -t ghcr.io/magier/ikt26/review-agent:dev .
#
# build-pr-repo.sh writes fixed authors and fixed commit dates, so the layer is
# byte-identical on every machine and in CI.
COPY .build/workshop-repo/ /workspace/workshop-repo/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKSHOP_REPO=/workspace/workshop-repo \
    WORKSHOP_NAMESPACE=ikt-workshop \
    KUBE_BACKEND=auto \
    AUDIT_LOG=/var/log/review-agent/events.jsonl \
    PORT=8080

# netshoot's WORKDIR is /root, which uid 10001 cannot write. Anything wanting a
# home - zsh, or a tool writing a dotfile - needs this pointed somewhere
# writable, or the exec-in-and-look-around workflow breaks. Its own line: a
# comment inside a line continuation is not portable across Dockerfile parsers.
ENV HOME=/tmp

RUN mkdir -p /var/log/review-agent && \
    adduser -D -u 10001 agent && \
    chown -R agent:agent /var/log/review-agent /workspace /app

# Numeric, so `runAsNonRoot: true` can be enforced without the kubelet having
# to resolve a name. Matches runAsUser in deploy/30-review-agent.yaml.
USER 10001

EXPOSE 8080
# netshoot ships CMD ["zsh"] and no ENTRYPOINT. Setting a real ENTRYPOINT is
# what lets deploy/30 drop its `command: ["/bin/sh","-c"]` workaround.
ENTRYPOINT ["python3", "-m", "reviewagent.cli"]
CMD ["serve"]
