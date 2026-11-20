import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { ApiError } from "./lib/api";
import { AuthProvider, useAuth } from "./lib/auth";
import { LiveProvider } from "./lib/liveContext";
import AnomaliesPage from "./pages/Anomalies";
import FaultLabPage from "./pages/FaultLab";
import GraphPage from "./pages/Graph";
import IncidentsPage from "./pages/Incidents";
import LoginPage from "./pages/Login";
import LogsPage from "./pages/Logs";
import OverviewPage from "./pages/Overview";
import RcaPage from "./pages/Rca";
import ServicesPage from "./pages/Services";
import SettingsPage from "./pages/Settings";
import TracesPage from "./pages/Traces";

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchInterval: 15_000,
        refetchOnWindowFocus: false,
        retry: (n, e) => !(e instanceof ApiError && [401, 403, 404, 422].includes(e.status)) && n < 2,
      },
    },
  });
}

function RequireAuth({ children }: { children: JSX.Element }) {
  const { user, ready } = useAuth();
  const loc = useLocation();
  if (!ready) return <Loading label="Restoring session" />;
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  return children;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<OverviewPage />} />
        <Route path="services" element={<ServicesPage />} />
        <Route path="services/:name" element={<ServicesPage />} />
        <Route path="traces" element={<TracesPage />} />
        <Route path="traces/:traceId" element={<TracesPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="anomalies" element={<AnomaliesPage />} />
        <Route path="rca" element={<RcaPage />} />
        <Route path="rca/:incidentId" element={<RcaPage />} />
        <Route path="graph" element={<GraphPage />} />
        <Route path="incidents" element={<IncidentsPage />} />
        <Route path="incidents/:id" element={<IncidentsPage />} />
        <Route path="lab" element={<FaultLabPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  const [qc] = useState(makeQueryClient);
  return (
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <LiveProvider>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </LiveProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
