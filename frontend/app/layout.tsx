import type { Metadata } from "next";
import "./globals.css";
import TopBar from "@/components/TopBar";

export const metadata: Metadata = { title: "Dropship Manager", description: "eBay sales, Amazon costs and sourcing" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <TopBar />
        <main className="page">{children}</main>
      </body>
    </html>
  );
}
