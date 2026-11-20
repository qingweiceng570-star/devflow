FROM node:24.21.0-alpine
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10.33.2 --activate
WORKDIR /workspace/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN --mount=type=cache,id=devflow-pnpm,target=/pnpm/store pnpm install --frozen-lockfile --store-dir=/pnpm/store --registry=$NPM_REGISTRY --fetch-retries=4 --fetch-timeout=300000
COPY frontend ./
RUN pnpm build
ENV NODE_ENV=production
CMD ["pnpm", "exec", "next", "start", "--hostname", "0.0.0.0", "--port", "3100"]
