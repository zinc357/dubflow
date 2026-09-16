import { useEffect, useState } from "react";
import { api, Health, Job } from "./api";
import HomeView from "./views/HomeView";
import WorkbenchView from "./views/WorkbenchView";

type View = { name: "home" } | { name: "workbench"; jobId: string };

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [view, setView] = useState<View>({ name: "home" });

  useEffect(() => {
    const checkHealth = () => api.health().then(setHealth).catch(() => setHealth(null));
    checkHealth();
    const th = window.setInterval(checkHealth, 2000); // 引擎由壳拉起，轮询等待其就绪
    const t = window.setInterval(() => {
      api.listJobs().then((r) => setJobs(r.jobs)).catch(() => {});
    }, 1000);
    return () => {
      window.clearInterval(t);
      window.clearInterval(th);
    };
  }, []);

  const openJob = (id: string) => setView({ name: "workbench", jobId: id });
  const workbenchJob =
    view.name === "workbench" ? jobs.find((j) => j.id === view.jobId) : undefined;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>DubFlow 视频翻译</h1>
        {health ? (
          <span className="badge ok">
            引擎已连接 · {health.backend.name}/{health.backend.device}
          </span>
        ) : (
          <span className="badge err">引擎未连接（127.0.0.1:8741）</span>
        )}
      </div>
      <p className="muted">
        流水线：导入 → 提取音频 → 语音识别（GPU）→ 翻译 → 导出 SRT。中间产物断点续跑。
      </p>

      {view.name === "home" ? (
        <HomeView jobs={jobs} onOpenJob={openJob} health={health} />
      ) : (
        <WorkbenchView
          jobId={view.jobId}
          job={workbenchJob}
          onBack={() => setView({ name: "home" })}
        />
      )}
    </>
  );
}
