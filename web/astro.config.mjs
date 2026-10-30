import { defineConfig } from "astro/config";

// SITE_URL and BASE_PATH come from actions/configure-pages at deploy time, so the
// site works at mendus59.github.io/is_this_normal and later on a custom domain.
export default defineConfig({
  site: process.env.SITE_URL || "http://localhost:4321",
  base: process.env.BASE_PATH || "/",
  output: "static",
});
