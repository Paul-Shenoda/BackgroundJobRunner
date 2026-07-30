import { useNav } from "../../context/NavContext";
import { useBreakpoint } from "../../hooks/useMediaQuery";
import Sidebar from "./Sidebar";
import BottomTabBar from "./BottomTabBar";
import TasksPage from "../../pages/TasksPage";
import SettingsPage from "../SettingsPage";
import ToastStack from "../toast/ToastStack";

export default function Shell() {
  const { view } = useNav();
  const { isPhone, isTablet } = useBreakpoint();

  const content = view === "settings" ? <SettingsPage /> : <TasksPage />;

  if (isPhone) {
    return (
      <div className="flex h-dvh flex-col bg-[color:var(--color-void)]">
        <div className="min-h-0 flex-1">{content}</div>
        <BottomTabBar />
        <ToastStack />
      </div>
    );
  }

  return (
    <div className="flex h-dvh bg-[color:var(--color-void)]">
      <Sidebar rail={isTablet} />
      <div className="min-h-0 flex-1">{content}</div>
      <ToastStack />
    </div>
  );
}
