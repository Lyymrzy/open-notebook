import type { NextConfig } from "next";

// Next.js dev server blocks cross-origin requests (including the HMR
// websocket) from any host not in this list, to guard against DNS
// rebinding. Set NEXT_ALLOWED_DEV_ORIGINS (comma-separated hostnames, no
// protocol/port) to access the dev server from a LAN IP or custom hostname.
const allowedDevOrigins = process.env.NEXT_ALLOWED_DEV_ORIGINS
  ? process.env.NEXT_ALLOWED_DEV_ORIGINS.split(",").map((s) => s.trim()).filter(Boolean)
  : undefined;

const nextConfig: NextConfig = {
  // Static export: the frontend is pre-rendered to `out/` and served by the
  // FastAPI backend on the same origin (no Node runtime at deploy time).
  output: "export",
  // Emit directory-style files (`/notebooks/index.html`) so a plain static file
  // server (FastAPI StaticFiles(html=True)) resolves deep links.
  trailingSlash: true,
  // next/image optimization needs a server; unused here but keep it explicit.
  images: { unoptimized: true },

  ...(allowedDevOrigins && allowedDevOrigins.length > 0 ? { allowedDevOrigins } : {}),
};


export default nextConfig;
