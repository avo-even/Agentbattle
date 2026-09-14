FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    FORWARDED_ALLOW_IPS=*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --proxy-headers plus FORWARDED_ALLOW_IPS above: Container Apps terminates TLS
# at the ingress, so without these the app sees the proxy rather than the client.
# Single worker is mandatory, not a default: all event state lives in memory,
# and a second worker would be a second, independent tournament.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
