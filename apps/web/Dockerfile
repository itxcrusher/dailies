# The board. Build context is apps/web, not the repo root:
#
#   docker build -t <tag> apps/web
#
# which is also why this one file is called `Dockerfile` while the Python images are
# `infra/Dockerfile.*` - `gcloud builds submit --tag` can only find a Dockerfile at the
# root of the source it uploads, and this app has a root of its own.

FROM node:22-slim AS deps
WORKDIR /app
# The lockfile, and `npm ci` rather than `npm install`: the image must install the exact
# tree that was tested, and `ci` fails loudly when the lockfile and package.json disagree
# instead of quietly resolving something newer.
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The standalone output: a server plus only the modules the build actually reached. The
# static assets are NOT included in it (Next expects them served from .next/static), so
# the second COPY is not optional - without it the page renders unstyled.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static

# node:22-slim ships an unprivileged `node` user; nothing here writes to disk.
USER node

EXPOSE 3000
# server.js is what `output: "standalone"` emits. It honours PORT and HOSTNAME, so Cloud
# Run injecting PORT is handled without a shell wrapper.
CMD ["node", "server.js"]
