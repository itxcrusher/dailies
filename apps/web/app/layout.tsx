import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dailies",
  description: "Delivery risk and diagnoses for shots on a render deadline",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
