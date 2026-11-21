FROM python:3-slim

# tar + pigz for (parallel) compression, tzdata for TZ=Europe/Berlin
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates \
      pigz \
      tar \
      tzdata && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Script is baked into the image, so the swarm node needs no copy of it on its disk.
# (No docker CLI needed: the script talks to the Docker socket directly.)

WORKDIR /scripts
COPY ./src/ /scripts/
RUN chmod +x /scripts/*.py
ENTRYPOINT ["python3", "-u", "/scripts/backup_minecraft_v4.py"]