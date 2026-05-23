FROM node:20-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        postgresql-client \
        coreutils \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# claude --dangerously-skip-permissions refuses to run as root; use the
# built-in unprivileged 'node' user (uid 1000) provided by node:20-slim.
RUN mkdir -p /input /output /work \
 && chown node:node /input /output /work

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER node
WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
