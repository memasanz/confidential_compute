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

# Ephemeral per-run workspace for agent code.
RUN mkdir -p /workspace
ENV SANDBOX_WORKSPACE=/workspace \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Fixed entrypoint: the CCE policy pins exactly this command.
CMD ["python", "server.py"]
