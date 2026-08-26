# Read-only Solis API for Home Assistant. No dashboard, no write path.
#   docker compose up -d     (see docker-compose.yml)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# dash.py is deliberately not copied: the container serves JSON only.
COPY solis_api.py solar_forecast.py octopus.py solis_net.py control.py ./

# Mutable state (solar_actuals.json, .solar_cache.json, energy_cost.json)
# lives on a volume so image rebuilds don't lose it.
ENV SOLIS_DATA_DIR=/data
VOLUME /data

EXPOSE 5051
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5051/api/health', timeout=4)"

CMD ["python", "solis_api.py"]
