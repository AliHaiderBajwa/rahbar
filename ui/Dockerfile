# RAHBAR chat UI: static files behind nginx, non-root, read-only root filesystem.
# Build context: ui/   ->   docker build -t rahbar-ui:dev ui
FROM nginxinc/nginx-unprivileged:1.30.6-alpine3.24
USER root
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
# nginx-unprivileged's uid is 101. nginx.conf writes only under /tmp (an emptyDir in k8s).
USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
