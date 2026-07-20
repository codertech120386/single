# Slim + stdlib-only: the image builds in seconds with no network, so a slow build can never be
# mistaken for slow placement when timing a scenario.
FROM python:3.12-alpine

WORKDIR /app
COPY app.py .

# Fleet reads PORT from the container env to find the app (see containerAddr in cmd/fleetd).
ENV PORT=8080 \
    SERVICE_NAME=single \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# HEALTHCHECK is docker-level and Fleet does NOT use it — Fleet probes over its own edge using the
# health check declared on the app (path/interval/thresholds). Kept anyway so `docker ps` is
# honest when running this by hand.
HEALTHCHECK --interval=5s --timeout=3s --retries=2 \
  CMD wget -qO- "http://127.0.0.1:${PORT}/healthz" >/dev/null || exit 1

CMD ["python", "app.py"]
