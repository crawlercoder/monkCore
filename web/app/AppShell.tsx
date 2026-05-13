"use client";

import type { ReactNode } from "react";

import { SiteNav } from "@/components/SiteNav";

/**
 * App shell: top ``SiteNav`` (Home, Jobs, Repos) and page content.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app-root">
      <SiteNav />
      {children}
    </div>
  );
}
