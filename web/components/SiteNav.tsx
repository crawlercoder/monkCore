import Link from "next/link";

/**
 * Top bar with button-styled links (Next ``Link`` + ``btn``) for main sections.
 */
export function SiteNav() {
  return (
    <header className="site-nav" role="navigation" aria-label="App sections">
      <div className="site-nav-inner">
        <Link href="/" className="btn btn-secondary site-nav-btn">
          Home
        </Link>
        <Link href="/jobs" className="btn btn-secondary site-nav-btn">
          Jobs
        </Link>
        <Link href="/repos" className="btn btn-secondary site-nav-btn">
          Repos
        </Link>
      </div>
    </header>
  );
}
