"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import SyncIndicator from "./SyncIndicator";

const LINKS = [
  { href: "/overview", label: "Overview" },
  { href: "/sourcing", label: "Sourcing" },
  { href: "/expenses", label: "Expenses" },
];

export default function TopBar() {
  const path = usePathname();
  return (
    <header className="topbar">
      <span className="brand">Dropship Manager</span>
      <nav className="nav">
        {LINKS.map((link) => (
          <Link key={link.href} href={link.href} className={path?.startsWith(link.href) ? "active" : ""}>
            {link.label}
          </Link>
        ))}
      </nav>
      <span className="spacer" />
      <SyncIndicator />
    </header>
  );
}
