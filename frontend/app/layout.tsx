import type { Metadata } from "next";
import "./globals.css";
import TopBar from "@/components/TopBar";

export const metadata: Metadata = { title: "Dropship Manager", description: "eBay sales, Amazon costs and sourcing" };

/**
 * Render the shared HTML document shell, navigation bar, and page content.
 *
 * @param children - React content for the active route, supplied through the props object.
 * @returns React element containing the top bar and the active page inside the main content area.
 */
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
