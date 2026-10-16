import { redirect } from "next/navigation";

/**
 * Redirect visitors from the root route to the overview dashboard.
 *
 * @remarks Takes no arguments.
 * @returns Never returns normally: Next.js redirect interrupts rendering and navigates to /overview.
 */
export default function Home() {
  redirect("/overview");
}
