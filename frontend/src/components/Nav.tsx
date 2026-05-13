import { NavLink } from "react-router-dom";

// The three pages from the brief, in the order you walk through them
// when onboarding a new team: create org → register a repo → watch it
// ingest.
const links = [
  { to: "/orgs/new", label: "1. Org Setup" },
  { to: "/repos/new", label: "2. Repo Setup" },
  { to: "/repos", label: "3. Repo List" },
];

export function Nav() {
  return (
    <nav className="nav">
      <div className="nav-inner">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            ◆
          </span>
          AI Agent · Console
        </div>
        <ul className="nav-links">
          {links.map((l) => (
            <li key={l.to}>
              <NavLink
                to={l.to}
                className={({ isActive }) =>
                  isActive ? "nav-link nav-link-active" : "nav-link"
                }
              >
                {l.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </div>
    </nav>
  );
}
