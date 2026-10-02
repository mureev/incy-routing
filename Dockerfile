# syntax=docker/dockerfile:1

# One stage: the service is the standard library and nothing else, so there is nothing to build and no
# builder worth keeping apart. If a dependency ever arrives, copy mureev/weather's two-stage venv layout.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INCY_DATA_DIR=/data

WORKDIR /app
COPY incy_routing/ ./incy_routing/

# Baked in so the running service can say what it is (/status), and CI can check it says this commit.
ARG APP_BUILD=dev
ARG APP_BUILT_AT=
ENV APP_BUILD=$APP_BUILD APP_BUILT_AT=$APP_BUILT_AT

# Nothing here needs root. /data is created owned by the service user so that a fresh named volume mounted
# there inherits that owner; it is deliberately not a VOLUME (compose keeps a declared one across recreates).
RUN useradd --system --uid 10001 --no-create-home incy && mkdir /data && chown incy:incy /data
USER incy

EXPOSE 8080

# Liveness only. Whether the profiles are fresh is /status's business: an upstream outage is not something a
# restart can fix, and the last good profiles keep being served meanwhile.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

CMD ["python", "-m", "incy_routing"]
