FROM node:20-slim
# git: one of Baileys' deps (libsignal) is pulled from GitHub
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY index.js ./
RUN mkdir -p /app/auth
CMD ["node", "index.js"]
