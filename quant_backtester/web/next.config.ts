import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Mounted under Pramana's single port by its gateway; one door, one host.
  basePath: "/lab",
  // The dashboard reads pipeline artifacts straight off disk, so pages must
  // never be statically cached at build time.
  experimental: {},
};

export default nextConfig;
