# The agent platform's control loop. Plain Python, no client library - it
# reaches the Kubernetes API with urllib and its mounted ServiceAccount token,
# which is the whole lesson made literal.
#
# distroless, not slim: this is the *hardened* container in the lab. No shell,
# no package manager, no tools, runs as nonroot. There is no exec surface to
# compromise here - which is exactly why the attacker has to go through the soft
# debug sidecar next to it (see k8s/orchestrator.yaml). The code is standard
# library only, so distroless/python3 runs it with nothing added.
FROM gcr.io/distroless/python3-debian12:nonroot

LABEL org.opencontainers.image.source="https://github.com/Magier/ikt26" \
      org.opencontainers.image.description="agent-orchestrator: spawns agent-worker pods via its ServiceAccount"

COPY server.py /app/server.py

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
# The base image's ENTRYPOINT is python3, so this runs `python3 /app/server.py`.
CMD ["/app/server.py"]
