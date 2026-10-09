FROM node:24.21.0-alpine
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10.33.2 --activate
WORKDIR /workspace/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend ./
CMD ["pnpm", "exec", "next", "dev", "--hostname", "0.0.0.0", "--port", "3100"]
