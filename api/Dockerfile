# RAHBAR api image. Build context: api/
#   docker build -t rahbar-api:dev api
# Two stages: dependencies are built in a throwaway layer, the final image has no pip
# cache, no compilers, and runs as an unprivileged user with a read-only root filesystem.
FROM python:3.12.16-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --upgrade pip==25.3 && /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.12.16-slim-trixie
# Pick up Debian security fixes released after the base image was built (Trivy gate).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rahbar \
    && pip uninstall -y pip setuptools wheel || true
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
EXPOSE 8000
# Exec form: uvicorn is PID 1 and receives SIGTERM directly, so the preStop sleep plus
# the graceful shutdown actually finish in-flight requests.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "20"]
