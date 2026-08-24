# Frontend image for Kubernetes.
# docker-compose bind-mounts ./frontend/dashboard + nginx.conf into a stock
# nginx container — that does not translate to K8s. This image BAKES the
# dashboard files in; the nginx config comes from the chart's ConfigMap
# (templates/frontend.yaml), so proxy targets use cluster DNS.
#
# Place this file at repo root as `frontend.Dockerfile` and build:
#   docker build -f frontend.Dockerfile -t REGISTRY/ai-classifier-frontend:<tag> .

FROM nginx:alpine

# Dashboard static files
COPY frontend/dashboard/ /usr/share/nginx/html/

# Remove the default server config; the chart mounts default.conf from a
# ConfigMap. (Keeping a fallback copy is optional — the ConfigMap wins.)
RUN rm -f /etc/nginx/conf.d/default.conf

EXPOSE 8082
