FROM python:3.13-alpine

LABEL org.opencontainers.image.title="AIOStreams Live Flows"
LABEL org.opencontainers.image.description="A live dashboard of who is streaming through AIOStreams"
LABEL org.opencontainers.image.source="https://github.com/pbalazs123/aiostreams-live-flows"
LABEL org.opencontainers.image.licenses="MIT"

COPY app /app
COPY scripts/build_geo.py /tmp/build_geo.py
RUN python /tmp/build_geo.py /app/geo && rm /tmp/build_geo.py && find /app -type d -name __pycache__ -prune -exec rm -rf {} +

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
EXPOSE 8300
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('FLOWS_PORT','8300')+'/health',timeout=4)"
CMD ["python", "/app/live_flows.py"]
