FROM node:24.15-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY auth.js server.js ./

ENV PORT=8080
CMD ["node", "server.js"]
