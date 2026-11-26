# syntax=docker/dockerfile:1

# Pin the same Xray release as tools/routing_check.sh. Copy only its executable.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS xray
ARG TARGETARCH
RUN python - "$TARGETARCH" <<'PYTHON'
import hashlib
import io
import os
import sys
import urllib.request
import zipfile

asset, digest = {
    "amd64": ("Xray-linux-64.zip", "1eb9175d0f0a8f8149c9230a7fc5ae66ce332ed20a53155ce61fe62e3f58b7df"),
    "arm64": ("Xray-linux-arm64-v8a.zip", "3e38d72dfc5eb65c91df0e5583e9b6676c32232041da47de6ae73946b526d66c"),
}[sys.argv[1]]
with urllib.request.urlopen(f"https://github.com/XTLS/Xray-core/releases/download/v26.9.9/{asset}", timeout=120) as response:
    archive = response.read()
if hashlib.sha256(archive).hexdigest() != digest:
    raise SystemExit("Xray archive checksum mismatch")
with zipfile.ZipFile(io.BytesIO(archive)) as release:
    release.extract("xray", "/")
    release.extract("LICENSE", "/")
os.chmod("/xray", 0o755)
PYTHON

FROM python:3.12-slim
COPY --from=xray /xray /usr/local/bin/xray
COPY --from=xray /LICENSE /usr/share/doc/xray/LICENSE

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
