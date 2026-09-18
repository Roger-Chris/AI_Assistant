FROM node:20-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY index.js ./
RUN mkdir -p /app/auth
CMD ["node", "index.js"]
