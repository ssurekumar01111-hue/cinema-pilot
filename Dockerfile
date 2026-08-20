FROM python:3.13-slim

WORKDIR /app

# Install ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only dashboard and required shared clients (no agent or Grafana MCP code)
COPY shared/graph_client /app/shared/graph_client
COPY shared/asset_storage /app/shared/asset_storage
COPY dashboard /app/dashboard

ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

CMD ["sh", "-c", "uvicorn dashboard.backend:app --host 0.0.0.0 --port ${PORT}"]
