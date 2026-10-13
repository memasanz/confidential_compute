# Pool gateway/controller image. Build context is a staged directory (see infra\pool\deploy-pool-apps.ps1)
# laid out as pool-control\, infra\pool\ (finalizer + worker template + policy-base.json) and
# mcp-build-sandbox\pool_worker.py, so no keys or operational files can enter the image.
# No Docker daemon or az confcom here: CCE policies are injected from the offline-generated base policy.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin pool
WORKDIR /app
COPY pool-control/requirements.txt /tmp/requirements.txt
RUN grep -vi '^pytest' /tmp/requirements.txt > /tmp/runtime.txt && pip install -r /tmp/runtime.txt
COPY pool-control/*.py /app/pool-control/
COPY infra/pool/ /app/infra/pool/
COPY mcp-build-sandbox/pool_worker.py /app/mcp-build-sandbox/
WORKDIR /app/pool-control
# Material volume mount point must be beneath the working directory (material.py).
RUN mkdir -p /app/pool-control/material && chown pool /app/pool-control/material
USER 10001
EXPOSE 8000
ENTRYPOINT ["python", "service.py"]
CMD ["gateway"]
