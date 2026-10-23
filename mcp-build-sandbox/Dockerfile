# Python build toolchain + MCP sandbox server.
FROM python:3.12-slim

# Tools the agent needs to build and test code inside the enclave.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git bash build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Data-analysis toolchain the agent's sessions inherit (pandas, Excel/CSV, tests).
RUN pip install --no-cache-dir pandas openpyxl pytest

COPY server.py .
COPY pool_worker.py .

# Generated code runs as this unprivileged user; the server (root) keeps /app and its
# environment unreadable to it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
    && chmod -R go-rwx /app

# Ephemeral per-run workspace for agent code.
RUN mkdir -p /workspace && chmod 755 /workspace
ENV SANDBOX_WORKSPACE=/workspace \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Fixed entrypoint: the CCE policy pins exactly this command.
CMD ["python", "server.py"]
