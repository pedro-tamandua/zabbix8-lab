FROM node:22-slim

WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY server.js .

# Auto-instrumentação OpenTelemetry (http, express, etc.)
CMD ["node", "--require", "@opentelemetry/auto-instrumentations-node/register", "server.js"]
