FROM node:22-alpine AS deps
WORKDIR /srv/web
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:22-alpine AS build
WORKDIR /srv/web
RUN corepack enable
COPY --from=deps /srv/web/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM node:22-alpine
WORKDIR /srv/web
RUN corepack enable
ENV NODE_ENV=production
COPY --from=build /srv/web/.next/standalone ./
COPY --from=build /srv/web/.next/static ./.next/static
COPY --from=build /srv/web/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
