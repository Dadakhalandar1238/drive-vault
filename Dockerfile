FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Render (and most free hosts) inject $PORT at runtime.
ENV PORT=8000
# --proxy-headers/--forwarded-allow-ips: this app sits behind a TLS-
# terminating reverse proxy (Render's own, or Caddy on the OCI deploy) --
# without this, uvicorn has no way to know the original request was
# HTTPS, so request.url_for() builds "http://" URLs (wrong OAuth
# redirect_uri, among other things). '*' is safe here: this container
# is never reachable directly, only through the proxy in front of it.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
