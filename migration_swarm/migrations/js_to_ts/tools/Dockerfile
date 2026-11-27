# Toolchain image for the js-to-ts pack: TypeScript compiler API + dependency-cruiser.
# Our own scripts (node/*.mjs) are mounted read-only at run time, not baked in.
FROM node:22-bookworm
WORKDIR /opt/msw
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund && chown -R 1000:1000 /opt/msw
ENV NODE_PATH=/opt/msw/node_modules
