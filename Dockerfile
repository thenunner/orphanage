FROM python:3.11-slim

# Speed up pip and keep image small
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /appdata/orphanage

# System deps (optional but helpful for some environments)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt /appdata/orphanage/requirements.txt
RUN pip install --no-cache-dir -r /appdata/orphanage/requirements.txt

# Start the API
EXPOSE 3750
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3750"]