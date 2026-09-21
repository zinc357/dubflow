import { useCallback, useEffect, useRef, useState } from "react";

const ENGINE = "http://127.0.0.1:8741";
const SIZES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"];
const STEP_LABELS: Record<string, string> = {
  probe: "探测", extract_audio: "提取音频", asr: "语音识别",
  translate: "翻译", export: "导出字幕",
};

export default function App() {
  const [health, setHealth] = useState<{ status: string; backend: { name: string; device: string } } | null>(null);
  const [jobs, setJobs] = useState<any[]>([]);
  const [videoPath, setVideoPath] = useState("");
  const [sourceLang, setSourceLang] = useState("");
  const [targetLang, setTargetLang] = useState("zh");
  const [device, setDevice] = useState("auto");
  const [size, setSize] = useState("large-v3-turbo");
  const [translate, setTranslate] = useState(false);
  const [trProvider, setTrProvider] = useState("google");
  const [subtitleVariant, setSubtitleVariant] = useState("bilingual");
  const [saveSrt, setSaveSrt] = useState(true);
  const [embedVideo, setEmbedVideo] = useState(false);
  const [outputDir, setOutputDir] = useState("");
  const [formError, setFormError] = useState("");

  useEffect(() => {
    const check = () =>
      fetch(ENGINE + "/health").then(r => r.json()).then(setHealth).catch(() => setHealth(null));
    check();
    const th = setInterval(check, 2000);
    const tj = setInterval(() =>
      fetch(ENGINE + "/jobs").then(r => r.json()).then(d => setJobs(d.jobs)).catch(() => {}), 1000);
    return () => { clearInterval(th); clearInterval(tj); };
  }, []);

  const submit = useCallback(async () => {
    setFormError("");
    try {
      const body: any = {
        video_path: videoPath,
        source_language: sourceLang || null,
        target_language: targetLang || "zh",
        asr: { provider: "auto", device, size },
        translation: {
          enabled: translate, provider: trProvider,
          ...(trProvider === "microsoft" ? {} : {}),
        },
        export: {
          variant: subtitleVariant, save_to_video_folder: saveSrt,
          embed_video: embedVideo, output_dir: outputDir || null,
        },
      };
      const r = await fetch(ENGINE + "/jobs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) setFormError(await r.text());
    } catch (e) { setFormError(String(e)); }
  }, [videoPath, sourceLang, targetLang, device, size, translate, trProvider, subtitleVariant, saveSrt, embedVideo, outputDir]);

  const stopJob = async (id: string) => {
    await fetch(`${ENGINE}/jobs/${id}/cancel`, { method: "POST" }).catch(() => {});
  };
  const removeJob = async (id: string) => {
    if (!window.confirm("确定删除？")) return;
    await fetch(`${ENGINE}/jobs/${id}`, { method: "DELETE" }).catch(() => {});
  };
  const clearFailed = async () => {
    if (!window.confirm("清除全部失败任务？")) return;
    await fetch(`${ENGINE}/jobs/clear-failed`, { method: "POST" }).catch(() => {});
  };

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ margin: 0 }}>DubFlow 视频翻译</h1>
        <span style={{
          padding: "3px 10px", borderRadius: 20, fontSize: 12,
          background: health ? "#d4edda" : "#f8d7da",
          color: health ? "#155724" : "#721c24",
        }}>
          {health ? `引擎已连接 · ${health.backend?.name}/${health.backend?.device}` : "引擎未连接"}
        </span>
      </div>
      <p style={{ color: "#666" }}>流水线：导入 → 提取音频 → 语音识别（GPU）→ 翻译 → 导出 SRT。</p>

      <h3>新建任务</h3>
      <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 16, marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <input style={{ flex: 1, padding: "6px 10px" }} placeholder="视频绝对路径"
            value={videoPath} onChange={e => setVideoPath(e.target.value)} />
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8, alignItems: "center" }}>
          <label>源语言</label>
          <select value={sourceLang} onChange={e => setSourceLang(e.target.value)}>
            <option value="">自动检测</option>
            {["en","zh","ja","ko","de","fr","es","ru"].map(l => <option key={l} value={l}>{l}</option>)}
          </select>
          <label>目标语言</label>
          <input style={{ width: 50 }} value={targetLang} onChange={e => setTargetLang(e.target.value)} />
          <label>运算设备</label>
          <select value={device} onChange={e => setDevice(e.target.value)}>
            <option value="auto">自动</option>
            <option value="gpu">GPU</option>
            <option value="cpu">CPU</option>
          </select>
          <label>模型大小</label>
          <select value={size} onChange={e => setSize(e.target.value)}>
            {["tiny","base","small","medium","large-v3","large-v3-turbo"].map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
        <div style={{ marginTop: 12 }}>
          <button disabled={!videoPath.trim()} onClick={submit}>开始处理</button>
          {formError && <span style={{ color: "red", marginLeft: 8 }}>{formError}</span>}
        </div>
      </div>

      <h3>任务列表</h3>
      <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 12 }}>
        {jobs.length === 0 && <p style={{ color: "#999" }}>暂无任务</p>}
        {jobs.map(j => {
          const vals = Object.values(j.steps as Record<string, { status: string; progress: number; detail: string }>);
          const done = vals.filter(s => s.status === "done" || s.status === "skipped").length;
          const running = vals.find(s => s.status === "running");
          const pct = (done / vals.length) * 100 + (running ? running.progress * (100 / vals.length) : 0);
          return (
            <div key={j.id} style={{ borderBottom: "1px solid #eee", padding: "8px 0" }}>
              <div style={{ fontWeight: 600 }}>{j.video_path} <small style={{ color: "#999" }}>#{j.id}</small></div>
              <div style={{ display: "flex", gap: 4, flexWrap: "wrap", margin: "6px 0" }}>
                {Object.entries(j.steps as Record<string, { status: string; progress: number }>).map(([k, s]) => (
                  <span key={k} style={{
                    padding: "2px 8px", borderRadius: 4, fontSize: 12,
                    background: s.status === "done" ? "#d4edda" : s.status === "running" ? "#cce5ff" : s.status === "failed" ? "#f8d7da" : "#eee",
                  }}>
                    {k}:{s.status}{s.status === "running" ? ` ${Math.round(s.progress * 100)}%` : ""}
                  </span>
                ))}
              </div>
              <div style={{ background: "#eee", borderRadius: 4, height: 6, marginTop: 6 }}>
                <div style={{ width: `${pct}%`, background: "#4f8cff", borderRadius: 4, height: "100%", transition: "width .3s" }} />
              </div>
              {j.error && <p style={{ color: "red", fontSize: 12 }}>{j.error}</p>}
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 20 }}>
        <button onClick={clearFailed} style={{ background: "#6c757d", color: "#fff", border: "none", borderRadius: 4, padding: "4px 12px" }}>
          清除失败任务
        </button>
        <button onClick={async () => {
          if (!window.confirm("确定删除所有任务？")) return;
          for (const j of jobs) {
            if (j.status === "failed" || j.status === "cancelled") await fetch(`${ENGINE}/jobs/${j.id}`, { method: "DELETE" });
          }
          window.location.reload();
        }} style={{ marginLeft: 8, background: "#6c757d", color: "#fff", border: "none", borderRadius: 4, padding: "4px 12px" }}>
          清除全部任务
        </button>
      </div>
    </div>
  );
}
