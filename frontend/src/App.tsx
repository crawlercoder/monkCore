import { Navigate, Route, Routes } from "react-router-dom";

import { Nav } from "./components/Nav";
import { OrgSetup } from "./pages/OrgSetup";
import { RepoList } from "./pages/RepoList";
import { RepoSetup } from "./pages/RepoSetup";

export function App() {
  return (
    <div className="app">
      <Nav />
      <main className="container">
        <Routes>
          <Route path="/" element={<Navigate to="/orgs/new" replace />} />
          <Route path="/orgs/new" element={<OrgSetup />} />
          <Route path="/repos/new" element={<RepoSetup />} />
          <Route path="/repos" element={<RepoList />} />
          <Route
            path="*"
            element={
              <div className="card">
                <h2>Not found</h2>
                <p className="muted">Pick a page from the navigation.</p>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
