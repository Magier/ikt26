# The agent platform's control loop. Plain Python, no client library - it
# reaches the Kubernetes API with urllib and its mounted ServiceAccount token,
# which is the whole lesson made literal.
#
# python:3.12-slim, not the netshoot/Alpine base: nothing here needs the network
# toolbox, and slim keeps the image small. The standard library is all the code
# uses, so there is nothing to pip install. Left on the tag, not a digest, like
# the agentbox image - pin it once the workshop's image set is frozen.
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/Magier/ikt26" \
      org.opencontainers.image.description="agent-orchestrator: spawns agent-worker pods via its ServiceAccount"

COPY server.py /app/server.py

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
CMD ["python3", "/app/server.py"]
