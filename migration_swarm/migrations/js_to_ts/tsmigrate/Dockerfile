# ts-migrate 0.1.35 (last release, 2022-11) with the TypeScript it was built against (4.x).
FROM node:18-bookworm
WORKDIR /opt/tsm
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund && chown -R 1000:1000 /opt/tsm
