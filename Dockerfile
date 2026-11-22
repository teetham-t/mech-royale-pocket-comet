FROM node:24.14.1-bookworm-slim@sha256:b506e7321f176aae77317f99d67a24b272c1f09f1d10f1761f2773447d8da26c
WORKDIR /bot
COPY src ./src
USER 65534:65534
ENTRYPOINT ["node", "/bot/src/pilot.ts"]
