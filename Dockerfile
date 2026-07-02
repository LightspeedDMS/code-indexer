# syntax=docker/dockerfile:1

# ---- Build stage ----
FROM node:22-bullseye-slim AS builder
WORKDIR /usr/src/app

COPY package*.json ./

RUN --mount=type=secret,id=npmrc,target=/root/.npmrc \
    HUSKY=0 npm ci

COPY src ./src
COPY nest-cli.json \
     tsconfig.json \
     tsconfig.build.json ./

RUN npm run build

# ---- Runtime stage ----
FROM node:22-bullseye-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY package*.json ./

RUN --mount=type=secret,id=npmrc,target=/root/.npmrc \
    npm ci --omit=dev --ignore-scripts

COPY --from=builder /usr/src/app/dist ./dist

USER node
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "dist/src/main.js"]
