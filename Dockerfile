# netshoot, plus a web console for driving it.
#
# Pinned by digest: netshoot builds from Alpine *edge*, so the same tag resolves
# to a different package set week to week. python3 and the network tools both
# come from the base - nothing is installed here.
FROM nicolaka/netshoot:v0.16@sha256:b09d9b21381f47a79b3cbcb30da25266dc17186ea00ae65e99fdc51396f48e70

# Attaches the GHCR package to the repository, so it appears under Packages.
LABEL org.opencontainers.image.source="https://github.com/Magier/ikt26" \
      org.opencontainers.image.description="netshoot with a web console for running commands in-pod"

COPY app/server.py /app/server.py

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    COMMAND_TIMEOUT=30

# Runs as root, like netshoot itself. tcpdump and nmap's raw-socket modes need
# it, and the whole point of the image is that those work. Tighten this when the
# base image is pruned down.
EXPOSE 8080
CMD ["python3", "/app/server.py"]
