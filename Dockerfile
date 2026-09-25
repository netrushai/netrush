# Runs anywhere that takes a container: Railway, Fly.io, Google Cloud Run, a VPS with Docker.
FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1 PORT=8000 TRUST_PROXY=1 DB_PATH=/data/netrush.db
RUN mkdir -p /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/healthz')"
CMD ["python", "server.py"]
