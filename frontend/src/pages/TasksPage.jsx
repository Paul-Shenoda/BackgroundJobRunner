import { useNav } from "../context/NavContext";
import { useBreakpoint } from "../hooks/useMediaQuery";
import FiltersBar from "../components/tasks/FiltersBar";
import TaskList from "../components/tasks/TaskList";
import TaskDetail from "../components/tasks/TaskDetail";
import NewTaskSheet from "../components/tasks/NewTaskSheet";
import { IconTasks } from "../components/icons";

export default function TasksPage() {
  const { selectedTask, closeDetail } = useNav();
  const { isDesktop } = useBreakpoint();

  if (isDesktop) {
    return (
      <div className="flex h-full min-h-0">
        <div className="flex h-full min-h-0 w-[420px] shrink-0 flex-col border-r border-[color:var(--color-hairline)]">
          <FiltersBar />
          <TaskList />
        </div>
        <div className="min-h-0 flex-1">
          {selectedTask ? (
            <TaskDetail task={selectedTask} />
          ) : (
            <EmptyDetail />
          )}
        </div>
        <NewTaskSheet />
      </div>
    );
  }

  // Tablet/phone: single pane. Detail is a full-screen push with a back
  // button rather than a second column -- 768px is too cramped for two.
  if (selectedTask) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <TaskDetail task={selectedTask} onBack={closeDetail} />
        <NewTaskSheet />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <FiltersBar />
      <TaskList />
      <NewTaskSheet />
    </div>
  );
}

function EmptyDetail() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 text-[color:var(--color-ink-faint)]">
      <IconTasks />
      <p className="text-sm">Select a task to see details</p>
    </div>
  );
}
