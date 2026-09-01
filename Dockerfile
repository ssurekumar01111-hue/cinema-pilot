FROM python:3.13-slim

WORKDIR /app

# ffmpeg renders the storyboard fallback and caption overlay inside the dashboard request
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Dashboard dependencies and the isolated Trailer Agent runtime
COPY shared/graph_client /app/shared/graph_client
COPY shared/asset_storage /app/shared/asset_storage
COPY shared/secret_client.py /app/shared/secret_client.py
COPY shared/concept_trailer.py /app/shared/concept_trailer.py
COPY shared/veo_client.py /app/shared/veo_client.py
COPY agents/trailer /app/agents/trailer
COPY dashboard /app/dashboard

ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

CMD ["sh", "-c", "uvicorn dashboard.backend:app --host 0.0.0.0 --port ${PORT}"]
