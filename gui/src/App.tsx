import { useCallback, useEffect, useState } from "react";
import WorkbenchView from "./views/WorkbenchView";

const ENGINE = "http://127.0.0.1:8741";

const STEP_LABELS: Record<string, string> = {
  probe: "探测", extract_audio: "提取音频", asr: "语音识别",
  translate: "翻译", export: "导出字幕",
};
const STATUS_LABELS: Record<string, string> = {
  done: "已完成", running: "进行中", failed: "失败",
  skipped: "已跳过", pending: "等待中", queued: "排队中", cancelled: "已取消",
};

interface Health {  backend: { name: string; device: string } }
interface StepInfo {  progress: number; detail: string }
import type { Job } from "./api";

export default function App() {
  const savedPrefs = (() => {
    try { return JSON.parse(localStorage.getItem("dubflow.prefs") ?? "{}"); } catch { return {}; }
  })();

  const [health, setHealth] = useState<Health | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [videoPath, setVideoPath] = useState("");
  const [device, setDevice] = useState(savedPrefs.device ?? "auto");
  const [size, setSize] = useState(savedPrefs.size ?? "large-v3-turbo");
  const [translate, setTranslate] = useState(savedPrefs.translate ?? false);
  const [trProvider, setTrProvider] = useState(savedPrefs.trProvider ?? "google");
  const [sourceLang, setSourceLang] = useState(savedPrefs.sourceLang ?? "");
  const [targetLang, setTargetLang] = useState(savedPrefs.targetLang ?? "zh");
  const [apiBase, setApiBase] = useState(savedPrefs.apiBase ?? "https://api.openai.com/v1");
  const [apiModel, setApiModel] = useState(savedPrefs.apiModel ?? "gpt-4o-mini");
  const [apiKey, setApiKey] = useState("");
  const [msftKey, setMsftKey] = useState("");
  const [msftRegion, setMsftRegion] = useState("global");
  const [subtitleVariant, setSubtitleVariant] = useState(savedPrefs.subtitleVariant ?? "bilingual");
  const [saveSrt, setSaveSrt] = useState(savedPrefs.saveSrt ?? true);
  const [embedVideo, setEmbedVideo] = useState(savedPrefs.embedVideo ?? false);
  const [outputDir, setOutputDir] = useState(savedPrefs.outputDir ?? "");
  const [error, setError] = useState("");
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [dl, setDl] = useState<{models: Array<{key:string; backend:string; downloaded:boolean; size_mb:number; dl_size_mb:number; status:string; progress:number}>} | null>(null);

  useEffect(() => {
    const check = () =>
      fetch(ENGINE + "/health").then(r => r.json()).then(setHealth).catch(() => setHealth(null));
    check();
    const th = setInterval(check, 2000);
    const tj = setInterval(() =>
      fetch(ENGINE + "/jobs").then(r => r.json()).then(d => setJobs(d.jobs)).catch(() => {}), 1000);
    const td = () => fetch(ENGINE + "/downloads").then(r => r.json()).then(setDl).catch(() => {});
    td();
    const tdi = setInterval(td, 2000);
    return () => { clearInterval(th); clearInterval(tj); clearInterval(tdi); };
  }, []);

  // 持久化表单设置：刷新/重开后恢复
  useEffect(() => {
    const prefs = { sourceLang, targetLang, device, size, translate, trProvider,
      apiBase, apiModel, subtitleVariant, saveSrt, embedVideo, outputDir };
    localStorage.setItem("dubflow.prefs", JSON.stringify(prefs));
  }, [sourceLang, targetLang, device, size, translate, trProvider, apiBase, apiModel, subtitleVariant, saveSrt, embedVideo, outputDir]);

  const startJob = useCallback(async () => {
    setError("");
    if (!videoFile && !videoPath.trim()) return;
    let serverPath = "";
    try {
      if (videoFile) {
        const fd = new FormData();
        fd.append("file", videoFile);
        const up = await fetch(ENGINE + "/upload", { method: "POST", body: fd });
        const upData = await up.json();
        serverPath = upData.path;
      } else {
        serverPath = videoPath.trim();
      }
      const body = {
        video_path: serverPath,
        source_language: sourceLang || null,
        target_language: targetLang || "zh",
        asr: { provider: "auto", device, model: size },
        translation: {
          enabled: translate, provider: trProvider,
          ...(trProvider === "llm" ? { base_url: apiBase, model: apiModel, ...(apiKey ? { api_key: apiKey } : {}) } : {}),
          ...(trProvider === "microsoft" && msftKey ? { api_key: msftKey, region: msftRegion } : {}),
        },
        export: {
          variant: translate ? subtitleVariant : "source",
          save_to_video_folder: saveSrt || embedVideo,
          ...(outputDir.trim() ? { output_dir: outputDir.trim() } : {}),
          embed_video: embedVideo,
        },
      };
      const r = await fetch(ENGINE + "/jobs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) setError(await r.text());
    } catch (e) { setError(String(e)); }
  }, [videoFile, videoPath, device, size, translate, trProvider, sourceLang, targetLang, apiBase, apiModel, apiKey, msftKey, msftRegion, subtitleVariant, saveSrt, embedVideo, outputDir]);

  const revealFile = useCallback(async (path: string) => {
    await fetch(ENGINE + "/fs/reveal", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }).catch(() => {});
  }, []);

  const deleteJob = useCallback(async (id: string) => {
    if (!window.confirm("确定删除该任务？中间产物将一并清除。")) return;
    await fetch(ENGINE + "/jobs/" + id, { method: "DELETE" }).catch(() => {});
  }, []);

  // 如果选中了任务，显示字幕编辑器
  if (selectedJobId) {
    const selectedJob = jobs.find(j => j.id === selectedJobId);
    return (
      <WorkbenchView
        jobId={selectedJobId}
        job={selectedJob}
        onBack={() => setSelectedJobId(null)}
      />
    );
  }

  return (
    <div style={{ fontFamily: "system-ui", maxWidth: 900, margin: "0 auto", padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2>DubFlow 视频翻译</h2>
        <span style={{ padding: "3px 10px", borderRadius: 20, fontSize: 12,
          background: health ? "#d4edda" : "#f8d7da", color: health ? "#155724" : "#721c24" }}>
          {health ? "引擎已连接" : "引擎未连接"}
        </span>
      </div>
      <h3>新建任务</h3>
      <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 16, marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <input type="text" placeholder="视频路径（也可点右侧上传）" value={videoPath}
            onChange={e => { setVideoPath(e.target.value); setVideoFile(null); }}
            style={{ flex: 1, padding: "6px 10px", border: "1px solid #ccc", borderRadius: 4 }} />
          <label style={{
            padding: "6px 16px", background: "#4f8cff", color: "#fff",
            borderRadius: 4, cursor: "pointer", whiteSpace: "nowrap", fontSize: 14,
          }}>
            📂 上传
            <input type="file" accept="video/*,audio/*" style={{ display: "none" }}
              onChange={e => {
                const f = e.target.files?.[0];
                if (f) { setVideoFile(f); setVideoPath(f.name); }
              }} />
          </label>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 8 }}>
          <label>源语言</label>
          <select value={sourceLang} onChange={e => setSourceLang(e.target.value)}>
            <option value="">自动检测</option>
            {["en","zh","ja","ko","de","fr","es","ru"].map(l => <option key={l} value={l}>{l}</option>)}
          </select>
          <label>目标语言</label>
          <select value={targetLang} onChange={e => setTargetLang(e.target.value)}>
            {["zh","en","ja","ko","de","fr","es","ru","pt","it","ar","hi","th","vi"].map(l => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <label>运算设备</label>
          <select value={device} onChange={e => setDevice(e.target.value)}>
            <option value="auto">自动</option>
            <option value="gpu">GPU</option>
            <option value="cpu">CPU</option>
          </select>
          <label>模型</label>
          <select value={size} onChange={e => setSize(e.target.value)}>
            {(dl?.models ?? []).filter(m => {
              const bn = health?.backend?.name ?? "";
              if (bn === "mlx-whisper") return m.backend === "mlx";
              if (bn === "whisper.cpp") return m.backend === "whisper.cpp" && m.key !== "whispercpp-vulkan-win64";
              return m.backend === "ctranslate2";
            }).sort((a, b) => {
              const order = ["tiny","base","small","medium","large-v3","large-v3-turbo"];
              const ra = order.indexOf(a.key.replace(/^(faster-whisper-|ggml-|)/, "").replace(/-q[45](_0)?$/, ""));
              const rb = order.indexOf(b.key.replace(/^(faster-whisper-|ggml-|)/, "").replace(/-q[45](_0)?$/, ""));
              return (ra === -1 ? 99 : ra) - (rb === -1 ? 99 : rb);
            }).map(m => (
              <option key={m.key} value={m.key}>
                {m.downloaded ? "✅" : "📥"} {m.key} ({m.downloaded ? m.size_mb : m.dl_size_mb} MB)
              </option>
            ))}
          </select>
          {(() => {
            const selected = dl?.models?.find(m => m.key === size);
            if (!selected || selected.downloaded) return null;
            return (
              <button
                onClick={() => fetch(ENGINE + "/downloads/models/" + size, { method: "POST" }).catch(() => {})}
                disabled={selected.status === "downloading"}
                style={{
                  padding: "2px 10px", border: "none", borderRadius: 4,
                  background: selected.status === "downloading" ? "#6c757d" : "#17a2b8",
                  color: "#fff", cursor: "pointer", fontSize: 12,
                }}>
                {selected.status === "downloading"
                  ? `下载中 ${Math.round(selected.progress * 100)}%`
                  : `下载 (${selected.dl_size_mb} MB)`}
              </button>
            );
          })()}
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
          <label style={{ fontWeight: translate ? 600 : 400 }}>
            <input type="checkbox" checked={translate} onChange={e => setTranslate(e.target.checked)} style={{ marginRight: 4 }} />
            翻译
          </label>
          {translate && (
            <select value={trProvider} onChange={e => setTrProvider(e.target.value)} style={{ marginLeft: 4 }}>
              <option value="google">谷歌（免费）</option>
              <option value="llm">LLM</option>
              <option value="microsoft">微软</option>
            </select>
          )}
          <label>字幕类型</label>
          <select value={translate ? subtitleVariant : "source"} onChange={e => setSubtitleVariant(e.target.value)}>
            <option value="source">仅原文</option>
            <option value="bilingual" disabled={!translate}>双语对照{!translate ? "（需开翻译）" : ""}</option>
            <option value="target" disabled={!translate}>仅译文{!translate ? "（需开翻译）" : ""}</option>
          </select>
          <label><input type="checkbox" checked={saveSrt} onChange={e => setSaveSrt(e.target.checked)} /> 保存字幕文件</label>
          <label><input type="checkbox" checked={embedVideo} onChange={e => setEmbedVideo(e.target.checked)} /> 烧录硬字幕</label>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
          <label>输出目录</label>
          <input type="text" placeholder="留空 = 视频所在文件夹" value={outputDir}
            onChange={e => setOutputDir(e.target.value)}
            style={{ flex: 1, padding: "4px 8px", border: "1px solid #ccc", borderRadius: 4 }} />
        </div>
        <div style={{ marginTop: 12 }}>
          <button disabled={!videoFile && !videoPath.trim()} onClick={startJob}>开始处理</button>
          {error && <span style={{ color: "red", marginLeft: 8 }}>{error}</span>}
        </div>
      </div>

      <h3>任务列表</h3>
      <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 12 }}>
        {jobs.length === 0 && <p style={{ color: "#999" }}>暂无任务</p>}
        {jobs.map(j => (
          <div key={j.id} style={{ borderBottom: "1px solid #eee", padding: "8px 0" }}>
            <div style={{ fontWeight: 600 }}>{j.video_path} <small style={{ color: "#999" }}>#{j.id}</small></div>
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap", margin: "4px 0" }}>
              {Object.entries(j.steps).map(([k, s]: [string, any]) => (
                <span key={k} style={{
                  padding: "2px 8px", borderRadius: 4, fontSize: 12,
                  background: s.status === "done" ? "#28a745" : s.status === "running" ? "#007bff" : s.status === "failed" ? "#dc3545" : s.status === "skipped" ? "#6c757d" : "#ffc107", color: "#fff", fontWeight: 500 }}>
                  {STEP_LABELS[k] ?? k}·{STATUS_LABELS[s.status] ?? s.status}{s.status === "running" ? ` ${Math.round(s.progress * 100)}%` : ""}
                </span>
              ))}
            </div>
            <div style={{ marginTop: 4, textAlign: "right" }}>
              {j.status === "done" && j.artifacts?.transcript && (
                <button
                  onClick={() => setSelectedJobId(j.id)}
                  style={{
                    padding: "2px 10px", border: "none", borderRadius: 4,
                    background: "#17a2b8", color: "#fff", cursor: "pointer",
                    fontSize: 12, marginRight: 4,
                  }}>
                  编辑字幕
                </button>
              )}
              {j.artifacts?.delivered_srt && (
                <button
                  onClick={() => revealFile(j.artifacts.delivered_srt)}
                  style={{
                    padding: "2px 10px", border: "none", borderRadius: 4,
                    background: "#28a745", color: "#fff", cursor: "pointer",
                    fontSize: 12, marginRight: 4,
                  }}>
                  📄 字幕文件
                </button>
              )}
              {j.artifacts?.embedded_video && (
                <button
                  onClick={() => revealFile(j.artifacts.embedded_video)}
                  style={{
                    padding: "2px 10px", border: "none", borderRadius: 4,
                    background: "#dc3545", color: "#fff", cursor: "pointer",
                    fontSize: 12, marginRight: 4,
                  }}>
                  🎬 硬字幕视频
                </button>
              )}
              <button
                onClick={() => deleteJob(j.id)}
                style={{
                  padding: "2px 10px", border: "none", borderRadius: 4,
                  background: j.status === "failed" || j.status === "cancelled" ? "#dc3545" : "#6c757d",
                  color: "#fff", cursor: "pointer", fontSize: 12,
                }}>
                删除
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}