FROM node:20-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        postgresql-client \
        coreutils \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Per-session build context contains:
#   repo.bundle       — git bundle of the user's repo
#   .credentials.json — Claude Code credentials (mode 0600)
COPY repo.bundle /input/repo.bundle
COPY .credentials.json /input/.credentials.json

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
