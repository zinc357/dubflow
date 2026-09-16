import { useCallback, useEffect, useState } from "react";
import { api, DownloadsSnapshot, EngineSettings, ENGINE_URL, Health, Job } from "../api";
import { STEP_LABELS } from "../labels";
import { loadFormPrefs, saveFormPrefs } from "../prefs";
import DirPicker from "../components/DirPicker";
import Tooltip from "../components/Tooltip";

// 可用模型随后端不同：mlx 只有 Apple Silicon 才有，ctranslate2 用于 Windows/Linux
// （NVIDIA CUDA 或 CPU 兜底）。这些键名必须与引擎 downloads.CATALOG 完全一致，
// 否则会出现「模型下载好了，引擎却找不到、转而去 HuggingFace 重下一遍」。
const MLX_MODELS = ["tiny", "medium", "large-v3", "large-v3-turbo", "large-v3-turbo-q4"];
const CT2_MODELS = [
  "faster-whisper-tiny",
  "faster-whisper-base",
  "faster-whisper-small",
  "faster-whisper-medium",
  "faster-whisper-large-v3",
  "faster-whisper-large-v3-turbo",
];
const DEFAULT_MLX_MODEL = "large-v3-turbo";
const DEFAULT_CT2_MODEL = "faster-whisper-large-v3-turbo";

function isAppleBackend(health?: Health | null): boolean {
  return health?.backend?.name === "mlx-whisper";
}

interface Props {
  jobs: Job[];
  onOpenJob: (id: string) => void;
  health?: Health | null;
}

export default function HomeView({ jobs, onOpenJob, health }: Props) {
  const modelList = isAppleBackend(health) ? MLX_MODELS : CT2_MODELS;
  // 上次用过的界面选项（浏览器本地）。翻译配置不放这里，由引擎侧保管。
  const [savedPrefs] = useState(loadFormPrefs);
  // new-task form
  const [videoPath, setVideoPath] = useState("");
  const [sourceLang, setSourceLang] = useState(savedPrefs.sourceLang ?? "");
  const [targetLang, setTargetLang] = useState(savedPrefs.targetLang ?? "zh");
  const [model, setModel] = useState(savedPrefs.model ?? DEFAULT_CT2_MODEL);
  const [translate, setTranslate] = useState(savedPrefs.translate ?? false);
  const [trProvider, setTrProvider] = useState(savedPrefs.trProvider ?? "llm");
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState("https://api.openai.com/v1");
  const [apiModel, setApiModel] = useState("gpt-4o-mini");
  const [msftKey, setMsftKey] = useState("");
  const [msftRegion, setMsftRegion] = useState("global");
  // 引擎侧是否已存有密钥。只回掩码 —— 输入框留空即表示沿用已保存的值。
  const [llmKeySet, setLlmKeySet] = useState(false);
  const [llmKeyHint, setLlmKeyHint] = useState("");
  const [msftKeySet, setMsftKeySet] = useState(false);
  const [msftKeyHint, setMsftKeyHint] = useState("");
  const [subtitleVariant, setSubtitleVariant] = useState(savedPrefs.subtitleVariant ?? "bilingual");
  // 默认不勾：不勾就什么都不落盘，要落盘得自己勾 —— 与直觉一致
  const [saveSrt, setSaveSrt] = useState(savedPrefs.saveSrt ?? false);
  const [embedVideo, setEmbedVideo] = useState(savedPrefs.embedVideo ?? false);
  const [outputDir, setOutputDir] = useState(savedPrefs.outputDir ?? "");
  const [pickDir, setPickDir] = useState(false);
  const [formError, setFormError] = useState("");
  const [dl, setDl] = useState<DownloadsSnapshot | null>(null);

  useEffect(() => {
    const t = window.setInterval(() => api.downloads().then(setDl).catch(() => {}), 2000);
    api.downloads().then(setDl).catch(() => {});
    return () => window.clearInterval(t);
  }, []);

  // 记住表单选项：下次打开就是上次的样子
  useEffect(() => {
    saveFormPrefs({
      sourceLang, targetLang, model, translate, trProvider,
      subtitleVariant, saveSrt, embedVideo, outputDir,
    });
  }, [sourceLang, targetLang, model, translate, trProvider,
      subtitleVariant, saveSrt, embedVideo, outputDir]);

  const applySettings = useCallback((s: EngineSettings) => {
    setApiBase(s.llm.base_url);
    setApiModel(s.llm.model);
    setLlmKeySet(s.llm.api_key_set);
    setLlmKeyHint(s.llm.api_key_hint);
    setMsftRegion(s.microsoft.region);
    setMsftKeySet(s.microsoft.key_set);
    setMsftKeyHint(s.microsoft.key_hint);
  }, []);

  // 进入页面时拉取引擎侧已保存的翻译配置
  useEffect(() => {
    api.getSettings().then(applySettings).catch(() => {});
  }, [applySettings]);

  // 后端探测结果回来后，把选中项切到该平台真正可用的模型（保留上次的有效选择）
  useEffect(() => {
    if (!health?.backend?.name) return;
    const apple = isAppleBackend(health);
    const list = apple ? MLX_MODELS : CT2_MODELS;
    setModel((cur) => (list.includes(cur) ? cur : apple ? DEFAULT_MLX_MODEL : DEFAULT_CT2_MODEL));
  }, [health?.backend?.name]);

  const clearSavedKey = useCallback(async (which: "llm" | "microsoft") => {
    if (!window.confirm("清除引擎侧已保存的密钥？以后需要重新填写。")) return;
    try {
      const s = which === "llm"
        ? await api.putSettings({ llm: { api_key: "" } })
        : await api.putSettings({ microsoft: { key: "" } });
      applySettings(s);
      if (which === "llm") setApiKey(""); else setMsftKey("");
    } catch (e) {
      setFormError(String(e));
    }
  }, [applySettings]);

  const submit = useCallback(async () => {
    setFormError("");
    try {
      // 先把翻译配置交给引擎保管（~/.dubflow/settings.json）：
      // 密钥框留空就是「沿用已保存的」，填了才覆盖；保存成功后立刻清空输入框，
      // 页面上不再残留明文，下次进来只显示掩码。
      const persisted = await api.putSettings({
        llm: {
          base_url: apiBase,
          model: apiModel,
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        },
        microsoft: {
          region: msftRegion,
          ...(msftKey.trim() ? { key: msftKey.trim() } : {}),
        },
      }).catch(() => null);
      if (persisted) {
        applySettings(persisted);
        setApiKey("");
        setMsftKey("");
      }

      await api.createJob({
        video_path: videoPath,
        source_language: sourceLang.trim() || null,
        target_language: targetLang.trim() || "zh",
        asr: { provider: "auto", model },
        translation: {
          enabled: translate,
          provider: trProvider,
          base_url: trProvider === "llm" ? apiBase || undefined : undefined,
          api_key: trProvider === "llm" ? apiKey || undefined
            : trProvider === "microsoft" ? msftKey || undefined : undefined,
          region: trProvider === "microsoft" ? msftRegion || undefined : undefined,
          model: trProvider === "llm" ? apiModel || undefined : undefined,
        },
        export: {
          variant: subtitleVariant,
          // 必须原样透传复选框状态。这里原来写的是 `saveSrt || embedVideo`，
          // 于是勾着「烧录」时即使取消勾选「保存字幕文件」，发出去的仍是 true ——
          // 复选框形同虚设，引擎会照旧把 .srt 交付出去。
          save_to_video_folder: saveSrt,
          embed_video: embedVideo,
          output_dir: outputDir.trim() || null,
        },
      });
    } catch (e) {
      setFormError(String(e));
    }
  }, [videoPath, sourceLang, targetLang, model, translate, trProvider, apiKey, apiBase, apiModel, msftKey, msftRegion, subtitleVariant, saveSrt, embedVideo, outputDir, applySettings]);

  const stopJob = useCallback(async (id: string) => {
    if (!window.confirm("确定停止该任务？已完成的步骤产物会保留。")) return;
    try {
      await api.cancelJob(id);
    } catch (e) {
      setFormError(String(e));
    }
  }, []);

  const removeJob = useCallback(async (id: string) => {
    if (!window.confirm("确定删除该任务？其转写/字幕等中间产物将一并清除。")) return;
    try {
      await api.deleteJob(id);
    } catch (e) {
      setFormError(String(e));
    }
  }, []);

  const clearFailed = useCallback(async () => {
    if (!window.confirm("确定清除全部失败/已取消的任务？")) return;
    try {
      await api.clearFailed();
    } catch (e) {
      setFormError(String(e));
    }
  }, []);

  const failedCount = jobs.filter((j) => j.status === "failed" || j.status === "cancelled").length;

  const overall = (j: Job) => {
    const vals = Object.values(j.steps);
    const done = vals.filter((s) => s.status === "done" || s.status === "skipped").length;
    const running = vals.find((s) => s.status === "running");
    return (done / vals.length) * 100 + (running ? running.progress * (100 / vals.length) : 0);
  };

  return (
    <>
      <h2>新建任务</h2>
      <div className="panel">
        <div className="row">
          <Tooltip
            className="grow"
            side="bottom"
            text="要处理的视频文件完整路径。可在文件管理器里 Shift+右键 →「复制文件地址」取得；粘贴时注意不要重复粘贴成两段。"
          >
            <input
              type="text"
              placeholder="视频绝对路径，如 C:\Users\me\Videos\demo.mp4"
              value={videoPath}
              onChange={(e) => setVideoPath(e.target.value)}
            />
          </Tooltip>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <Tooltip text="视频里的说话语言。不确定就保持「自动检测」，模型判断得很准。" side="bottom">
            <label className="muted">源语言</label>
          </Tooltip>
          <Tooltip text="手动指定源语言可以略快且更稳，适合口音重或中英混杂的内容。" side="bottom">
            <select value={sourceLang} onChange={(e) => setSourceLang(e.target.value)}>
              <option value="">自动检测</option>
              {["en", "zh", "ja", "ko", "de", "fr", "es", "ru"].map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </select>
          </Tooltip>
          <Tooltip text="翻译的目标语言代码，例如 zh（中文）、en（英文）、ja（日文）。" side="bottom">
            <label className="muted">目标语言</label>
          </Tooltip>
          <Tooltip text="字幕要翻译成的语言代码。开启翻译后按这个值输出译文。" side="bottom">
            <input
              type="text"
              style={{ flex: 0, width: 60 }}
              value={targetLang}
              onChange={(e) => setTargetLang(e.target.value)}
            />
          </Tooltip>
          <Tooltip text="语音识别模型。越大越准也越慢；large-v3-turbo 是质量与速度的平衡点。首次使用需在下方「模型与依赖」里下载。" side="bottom">
            <label className="muted">识别模型</label>
          </Tooltip>
          <Tooltip
            text={`列表已按当前后端（${isAppleBackend(health) ? "mlx · Apple Silicon" : "CTranslate2 · NVIDIA CUDA 或 CPU"}）过滤，共 ${modelList.length} 个可用模型。`}
            side="bottom"
          >
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {modelList.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </Tooltip>
          <Tooltip text="开启后会在识别完成后调用翻译服务生成译文字幕，需要填好下面的 API 信息。" side="bottom">
            <label className="row" style={{ gap: 4 }}>
              <input
                type="checkbox"
                style={{ width: "auto" }}
                checked={translate}
                onChange={(e) => setTranslate(e.target.checked)}
              />
              <span className="muted">翻译</span>
            </label>
          </Tooltip>
          {translate && (
            <Tooltip text="LLM 翻译质量最好但需要 API Key；谷歌免费无需 Key，但大陆要代理且易被限流；微软需要 Azure 订阅密钥。" side="bottom">
              <select value={trProvider} onChange={(e) => setTrProvider(e.target.value)}>
                <option value="llm">LLM 翻译</option>
                <option value="google">谷歌翻译</option>
                <option value="microsoft">微软翻译</option>
              </select>
            </Tooltip>
          )}
        </div>
        {translate && trProvider === "llm" && (
          <div className="row" style={{ marginTop: 10 }}>
            <Tooltip style={{ flex: 2 }} side="bottom" text="OpenAI 兼容接口地址，务必带上版本路径。例：https://api.deepseek.com/v1">
              <input type="text" value={apiBase} onChange={(e) => setApiBase(e.target.value)} placeholder="API Base URL" />
            </Tooltip>
            <Tooltip style={{ flex: 1 }} side="bottom" text="模型名。例：deepseek-chat、qwen-plus、glm-4-flash。">
              <input type="text" value={apiModel} onChange={(e) => setApiModel(e.target.value)} placeholder="模型名" />
            </Tooltip>
            <Tooltip
              style={{ flex: 1 }}
              side="bottom"
              text={
                llmKeySet
                  ? `已保存在引擎侧（${llmKeyHint}），此处留空即表示继续使用它。密钥只回掩码，不会以明文出现在页面上。`
                  : "服务商提供的密钥。填过一次后会被引擎记住，下次打开无需重填。"
              }
            >
              <input
                type="text"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={llmKeySet ? `已保存 ${llmKeyHint} · 留空即用` : "API Key"}
              />
            </Tooltip>
            {llmKeySet && (
              <Tooltip side="bottom" text="清除引擎侧已保存的 API Key，清除后需要重新填写。">
                <button style={{ padding: "4px 10px" }} onClick={() => clearSavedKey("llm")}>
                  清除
                </button>
              </Tooltip>
            )}
          </div>
        )}
        {translate && trProvider === "microsoft" && (
          <div className="row" style={{ marginTop: 10 }}>
            <Tooltip
              style={{ flex: 2 }}
              side="bottom"
              text={
                msftKeySet
                  ? `已保存在引擎侧（${msftKeyHint}），此处留空即表示继续使用它。密钥只回掩码，不会以明文出现在页面上。`
                  : "Azure 门户里 Translator 资源的密钥，免费 F0 档即可（每月 200 万字符）。填过一次后会被引擎记住。"
              }
            >
              <input
                type="text"
                value={msftKey}
                onChange={(e) => setMsftKey(e.target.value)}
                placeholder={msftKeySet ? `已保存 ${msftKeyHint} · 留空即用` : "Azure Translator Key（免费 F0 档即可）"}
              />
            </Tooltip>
            <Tooltip
              style={{ flex: 0, width: 120 }}
              side="bottom"
              text="资源所在的区域标识，例如 global、eastasia。填错会返回 401。"
            >
              <input type="text" value={msftRegion} onChange={(e) => setMsftRegion(e.target.value)} placeholder="区域，如 global" />
            </Tooltip>
            {msftKeySet && (
              <Tooltip side="bottom" text="清除引擎侧已保存的 Azure 密钥，清除后需要重新填写。">
                <button style={{ padding: "4px 10px" }} onClick={() => clearSavedKey("microsoft")}>
                  清除
                </button>
              </Tooltip>
            )}
          </div>
        )}
        {translate && trProvider === "google" && (
          <div className="muted" style={{ marginTop: 8 }}>谷歌免费接口，经系统代理访问，无需 key。</div>
        )}
        <div className="row" style={{ marginTop: 10 }}>
          <Tooltip text="选择要导出的字幕形式。注意「仅译文」和「双语对照」都必须先开启上面的翻译。" side="bottom">
            <label className="muted">字幕类型</label>
          </Tooltip>
          <Tooltip text="双语对照 = 原文一行 + 译文一行；仅译文 = 只保留翻译结果；仅原文 = 只保留识别结果。" side="bottom">
            <select value={subtitleVariant} onChange={(e) => setSubtitleVariant(e.target.value)}>
              <option value="bilingual">双语对照</option>
              <option value="target">仅译文</option>
              <option value="source">仅原文</option>
            </select>
          </Tooltip>
          <Tooltip text="勾选后会把字幕文件复制一份到输出目录，视频本身不动。不勾则不落任何字幕文件。" side="bottom">
            <label className="row" style={{ gap: 4 }}>
              <input
                type="checkbox"
                style={{ width: "auto" }}
                checked={saveSrt}
                onChange={(e) => setSaveSrt(e.target.checked)}
              />
              <span className="muted">保存字幕文件</span>
            </label>
          </Tooltip>
          <Tooltip text="用 ffmpeg 把字幕烧进画面，生成一个新视频。需要重新编码，比较耗时。它与「保存字幕文件」互不影响：只要这份烧录视频，就不必勾保存字幕。" side="bottom">
            <label className="row" style={{ gap: 4 }}>
              <input
                type="checkbox"
                style={{ width: "auto" }}
                checked={embedVideo}
                onChange={(e) => setEmbedVideo(e.target.checked)}
              />
              <span className="muted">烧录硬字幕生成新视频</span>
            </label>
          </Tooltip>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <Tooltip text="字幕文件和硬字幕视频的保存位置。留空则保存到原视频所在文件夹。" side="bottom">
            <label className="muted">输出目录</label>
          </Tooltip>
          <input
            type="text"
            value={outputDir}
            placeholder="留空 = 原视频所在文件夹"
            onChange={(e) => setOutputDir(e.target.value)}
          />
          <button className="ghost" onClick={() => setPickDir(true)}>
            浏览…
          </button>
          {outputDir && (
            <button className="ghost" onClick={() => setOutputDir("")}>
              恢复默认
            </button>
          )}
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <Tooltip text="建立任务并立即开始处理。重复处理同一个视频会从头再跑一遍全部步骤。" side="bottom">
            <button disabled={!videoPath.trim()} onClick={submit}>
              开始处理
            </button>
          </Tooltip>
          {formError && <span className="error">{formError}</span>}
        </div>
      </div>

      <div className="row" style={{ justifyContent: "space-between", margin: "18px 0 8px" }}>
        <h2 style={{ margin: 0 }}>任务列表</h2>
        {failedCount > 0 && (
          <Tooltip align="right" side="bottom" text="一次性删除所有失败和已停止的任务，连同它们的中间产物。">
            <button style={{ padding: "2px 10px" }} onClick={clearFailed}>
              清除失败任务（{failedCount}）
            </button>
          </Tooltip>
        )}
      </div>
      <div className="panel">
        {jobs.length === 0 && <span className="muted">暂无任务</span>}
        {jobs.map((j) => {
          const vals = Object.values(j.steps);
          const doneCount = vals.filter((s) => s.status === "done" || s.status === "skipped").length;
          const running = vals.find((s) => s.status === "running");
          const pct = (doneCount / vals.length) * 100 + (running ? running.progress * (100 / vals.length) : 0);
          const canEdit = !!j.artifacts?.transcript;
          return (
            <div className="job" key={j.id}>
              <div className="file">
                {j.video_path}
                <span className="muted" style={{ marginLeft: 8 }}>#{j.id}</span>
              </div>
              <div className="steps">
                {Object.entries(j.steps).map(([k, s]) => (
                  <span key={k} className={`step ${s.status}`}>
                    {STEP_LABELS[k] ?? k}·{s.status}
                    {s.status === "running" ? ` ${Math.round(s.progress * 100)}%` : ""}
                  </span>
                ))}
                {j.backend && "name" in j.backend && (
                  <span className="step">{j.backend.name}/{j.backend.device}</span>
                )}
                <span style={{ marginLeft: "auto" }} />
                {(j.status === "running" || j.status === "queued") && (
                  <Tooltip
                    align="right"
                    side="bottom"
                    text="停止该任务。识别会在当前这段音频处理完后中断；已经完成的步骤产物都会保留。"
                  >
                    <button
                      className="ghost"
                      style={{ padding: "2px 10px" }}
                      onClick={() => stopJob(j.id)}
                    >
                      停止
                    </button>
                  </Tooltip>
                )}
                <Tooltip
                  align="right"
                  side="bottom"
                  text={
                    canEdit
                      ? "打开字幕工作台：逐句校对文本、播放原声、拆分合并，再重新导出。"
                      : "语音识别完成后才能编辑字幕。"
                  }
                >
                  <button
                    style={{ padding: "2px 10px" }}
                    disabled={!canEdit}
                    onClick={() => onOpenJob(j.id)}
                  >
                    编辑字幕
                  </button>
                </Tooltip>
                {(j.status === "failed" || j.status === "cancelled") && (
                  <Tooltip
                    align="right"
                    side="bottom"
                    text="删除该任务，并清掉它在 ~/.dubflow/jobs/ 下的中间产物。只有失败或已停止的任务可以删除。"
                  >
                    <button
                      style={{ padding: "2px 10px" }}
                      onClick={() => removeJob(j.id)}
                    >
                      删除
                    </button>
                  </Tooltip>
                )}
              </div>
              <div className="bar"><div style={{ width: `${pct}%` }} /></div>
              {j.error && <div className="error">{j.error}</div>}
            </div>
          );
        })}
      </div>

      <details>
        <summary className="muted" style={{ cursor: "pointer", margin: "18px 0 8px" }}>模型与依赖</summary>
        <div className="panel">
          <div className="row">
            <b>ffmpeg（捆绑）</b>
            {dl?.ffmpeg.installed ? (
              <span className="badge ok">已就绪</span>
            ) : (
              <span className="badge err">未下载</span>
            )}
            {dl && !dl.ffmpeg.installed && dl.ffmpeg.status !== "downloading" && (
              <Tooltip side="bottom" text="下载带 libass 的静态 ffmpeg 到项目 bin/ 目录。如果系统 PATH 里已经装了完整版 ffmpeg，其实不必下载。">
                <button onClick={() => api.downloadFfmpeg()}>下载当前平台 ffmpeg</button>
              </Tooltip>
            )}
            {dl && dl.ffmpeg.status === "downloading" && (
              <span className="muted">
                下载中 {Math.round(dl.ffmpeg.progress * 100)}% {dl.ffmpeg.detail}
              </span>
            )}
            {dl && dl.ffmpeg.status === "failed" && (
              <span className="error">{dl.ffmpeg.detail}</span>
            )}
          </div>
          <table style={{ marginTop: 12 }}>
            <thead>
              <tr><th>模型</th><th>后端</th><th>本地大小</th><th>状态</th><th></th></tr>
            </thead>
            <tbody>
              {dl?.models.map((m) => (
                <tr key={m.key}>
                  <td>{m.key}</td>
                  <td>{m.backend}</td>
                  <td>{m.size_mb} MB</td>
                  <td>
                    {m.downloaded
                      ? "已下载"
                      : m.status === "downloading"
                      ? `下载中 ${Math.round(m.progress * 100)}%`
                      : m.status === "failed"
                      ? <span className="error">失败: {m.detail}</span>
                      : "未下载"}
                  </td>
                  <td>
                    {!m.downloaded && (
                      <Tooltip
                        align="right"
                        side="bottom"
                        text={`下载 ${m.key} 到 ~/.dubflow/models/ 下。下载源会按 ModelScope → HF 镜像依次尝试。`}
                      >
                        <button
                          style={{ padding: "2px 10px" }}
                          disabled={m.status === "downloading"}
                          onClick={() => api.downloadModel(m.key)}
                        >
                          下载
                        </button>
                      </Tooltip>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ marginBottom: 0 }}>
            下载源自动优先 ModelScope（国内直连）→ HF 镜像。
            <b> mlx</b> 仅 Apple Silicon 可用；<b>ctranslate2</b> 用于 Windows/Linux（NVIDIA CUDA，检测不到显卡时自动回落 CPU int8）；
            <b>whisper.cpp</b> 面向 AMD/Intel 的 Vulkan 路线。
            模型名需与上方「识别模型」下拉里的选项一致，引擎才能命中已下载的本地模型。
          </p>
        </div>
      </details>

      {pickDir && (
        <DirPicker
          value={outputDir || undefined}
          onPick={setOutputDir}
          onClose={() => setPickDir(false)}
        />
      )}
    </>
  );
}
