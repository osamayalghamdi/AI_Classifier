# Frontend image (dashboard) for Kubernetes.
#
# In compose the dashboard is bind-mounted into a stock nginx (see the `nginx`
# service in docker-compose.yml) — that does not translate to K8s, so this
# image BAKES the dashboard files in.
#
# The nginx *config* is NOT owned by this image:
#   · k8s      → the chart's ConfigMap is mounted at /etc/nginx/conf.d/default.conf
#                (subPath default.conf), replacing the fallback below, so proxy
#                targets use cluster DNS;
#   · compose  → frontend/nginx.conf is bind-mounted at the same path.
# The fallback below exists only so the image also works on its own
# (docker run -p 8082:8082 …) instead of starting nginx with no server block.
#
# Build:  docker build -f frontend/Dockerfile -t REGISTRY/ai-classifier-frontend:<tag> frontend/

FROM nginx:alpine

# Dashboard static files
COPY dashboard/ /usr/share/nginx/html/

# Fallback server block (replaced by the chart ConfigMap / compose bind mount
# in every real deployment). Static dashboard only: it does NOT proxy the API —
# that wiring belongs to the cluster's config.
RUN printf '%s\n' \
      'server {' \
      '    listen 8082;' \
      '    server_name _;' \
      '    root /usr/share/nginx/html;' \
      '    index index.html;' \
      '    location = /healthz { return 200 "ok"; }' \
      '    location / { try_files $uri $uri/ /index.html; }' \
      '}' > /etc/nginx/conf.d/default.conf

EXPOSE 8082
