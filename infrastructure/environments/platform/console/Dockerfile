# Platform console: platform-frontend's static build behind nginx, with /api forwarded to the
# control-plane (prefix stripped, same origin as the frontend expects).
# Build with the platform-frontend repository as the context:
#   docker buildx build --platform linux/amd64 -f environments/platform/console/Dockerfile \
#     --build-context console=environments/platform/console -t <registry>/platform-console:<tag> ../platform-frontend
FROM node:24.19.0-bookworm-slim AS build
WORKDIR /web
RUN corepack enable
COPY . .
RUN pnpm install --frozen-lockfile && pnpm run build

FROM nginx:1.27-alpine
COPY --from=console nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
