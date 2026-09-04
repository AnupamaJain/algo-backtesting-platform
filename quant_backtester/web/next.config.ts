import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard reads pipeline artifacts straight off disk, so pages must
  // never be statically cached at build time.
  experimental: {},
};

export default nextConfig;
