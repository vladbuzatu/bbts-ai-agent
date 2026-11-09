# Production image for the Angular app, served by Caddy. Built by GitHub Actions.
FROM node:22-alpine AS build
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ .
RUN npx ng build --configuration production \
 && mkdir /out \
 && (cp -r dist/*/browser/. /out/ 2>/dev/null || cp -r dist/*/. /out/)

FROM caddy:2-alpine
ARG APP_VERSION=dev
COPY --from=build /out /srv
# Public, harmless build info: lets the bot show which version is really running.
RUN printf '{"version":"%s"}\n' "$APP_VERSION" > /srv/version.json
COPY deploy/web.Caddyfile /etc/caddy/Caddyfile
