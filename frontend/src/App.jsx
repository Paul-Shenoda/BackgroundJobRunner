import { AuthProvider, useAuth } from "./context/AuthContext";
import { EventStreamProvider } from "./context/EventStreamContext";
import { NavProvider } from "./context/NavContext";
import { TasksProvider } from "./context/TasksContext";
import { ToastProvider } from "./context/ToastContext";
import { ThemeProvider } from "./context/ThemeContext";
import FirstRunCard from "./components/FirstRunCard";
import Shell from "./components/layout/Shell";

function AuthedApp() {
  const { apiKey } = useAuth();

  if (!apiKey) return <FirstRunCard />;

  // Keying the whole authenticated subtree on apiKey forces a full
  // unmount/remount on switch (Settings -> change key) -- every provider
  // resets and the SSE stream reconnects under the new key, rather than
  // trying to patch a dozen pieces of state in place.
  return (
    <div key={apiKey}>
      <EventStreamProvider>
        <NavProvider>
          <TasksProvider>
            <ToastProvider>
              <Shell />
            </ToastProvider>
          </TasksProvider>
        </NavProvider>
      </EventStreamProvider>
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AuthedApp />
      </AuthProvider>
    </ThemeProvider>
  );
}
