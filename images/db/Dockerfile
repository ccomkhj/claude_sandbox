FROM postgres:16

ENV POSTGRES_HOST_AUTH_METHOD=trust \
    POSTGRES_DB=appdb \
    POSTGRES_USER=postgres

# The dump file is copied in by the CLI before `docker build`.
COPY dump.dump /docker-entrypoint-initdb.d/00_dump.dump
COPY init.sh /docker-entrypoint-initdb.d/01_restore.sh
RUN chmod +x /docker-entrypoint-initdb.d/01_restore.sh
