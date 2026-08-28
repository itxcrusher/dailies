import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone: the build emits a self-contained server plus only the node_modules it
  // actually reached, so the runtime image copies ~50 MB instead of the full install and
  // never runs `npm` at all. Cloud Run charges for cold starts, and the image size is
  // most of one.
  output: "standalone",
};

export default nextConfig;
