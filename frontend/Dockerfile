# Node 只存在于构建阶段，生产 Caddy 镜像只提供静态 Vue 产物。
FROM node:24-bookworm-slim AS build
WORKDIR /app
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend ./
RUN pnpm build

FROM docker.io/library/caddy:2.10-alpine AS runtime
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
