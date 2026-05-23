FROM ubuntu/squid:latest

COPY entrypoint.sh /sandbox-entrypoint.sh
RUN chmod +x /sandbox-entrypoint.sh

ENTRYPOINT ["/sandbox-entrypoint.sh"]
