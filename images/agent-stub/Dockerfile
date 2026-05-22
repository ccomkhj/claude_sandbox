FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        postgresql-client \
        curl \
        coreutils \
 && rm -rf /var/lib/apt/lists/*

COPY repo.bundle /input/repo.bundle
COPY .credentials.json /input/.credentials.json
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
