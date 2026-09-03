import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dailies",
  description:
    "A render exits 0 and the jacket is magenta. Dailies reads render telemetry through the Grafana MCP server, then opens the frame itself to check the two answers agree.",
  // Next resolves app/icon.svg, app/apple-icon.png and app/opengraph-image.png by
  // convention; these give the link preview the words to go with the picture.
  openGraph: {
    title: "Dailies",
    description:
      "A Gemini agent reads render telemetry through the Grafana MCP server, then opens the frame itself to check the two answers agree.",
    type: "website",
  },
  twitter: { card: "summary_large_image" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/* Archivo for display, Roboto Mono for every figure. Preconnected because the
            masthead is the first thing painted and a late swap on it is very visible. */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Archivo:ital,wght@0,500;0,700;0,800;1,800&family=Roboto+Mono:wght@400;500;700&display=swap"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
