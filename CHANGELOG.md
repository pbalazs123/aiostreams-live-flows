# Changelog

## 0.2.0 (4 November 2026)

- A layout for phones and tablets, with flow stripes on phones
- Can be installed as an app (over HTTPS)
- The chart and recent events survive restarts, in a new Docker volume. If you use your own Compose file, add a volume at `/data`
- A status light on the AIOStreams bubble, and bubbles of equal height
- Shorter CPU usage text

## 0.1.0 (2 November 2026)

First version.

- Live viewers, sources, speed, addon health, server load, daily traffic and finished streams, from AIOStreams alone
- Demo mode with simulated data
- Page password and a server-side setting to hide viewers
- Docker image for amd64 and arm64, rebuilt monthly for new country data
