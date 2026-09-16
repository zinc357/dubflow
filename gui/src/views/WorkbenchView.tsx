import { useCallback, useEffect, useRef, useState } from "react";
import { api, EditableSegment, ENGINE_URL, Job } from "../api";
import { STEP_LABELS } from "../labels";
import DirPicker from "../components/DirPicker";
import Tooltip from "../components/Tooltip";

interface Props {
  jobId: string;
  job: Job | undefined;
  onBack: () => void;
}

interface EditorData {
  segments: EditableSegment[];
  translations: string[];
}

export default function WorkbenchView({ jobId, job, onBack }: Props) {
  const [editor, setEditor] = useState<EditorData | null>(null);
  const [loadError, setLoadError] = useState("");
  const [audioRef] = useState<{ current: HTMLAudioElement | null }>({ current: null });
  const [playingIndex, setPlayingIndex] = useState<number | null>(null);
  const [reexportVariant, setReexportVariant] = useState("bilingual");
  const [reexportSave, setReexportSave] = useState(false);
  const [reexportEmbed, setReexportEmbed] = useState(false);
  const [reexportDir, setReexportDir] = useState("");
  const [pickDir, setPickDir] = useState(false);
  const [reexportPicked, setReexportPicked] = useState(false);

  // 任务数据到手后，用该任务原本的导出选择作为「重新导出」的默认值。
  // 只同步一次，之后用户自己改的不会被轮询覆盖。
  useEffect(() => {
    if (reexportPicked || !job?.export_options) return;
    const eo = job.export_options;
    if (eo.variant) setReexportVariant(eo.variant);
    setReexportSave(eo.save_to_video_folder ?? false);
    if (eo.embed_video) setReexportEmbed(true);
    if (eo.output_dir) setReexportDir(eo.output_dir);
    setReexportPicked(true);
  }, [job, reexportPicked]);

  const loadTranscript = useCallback(async () => {
    try {
      const tr = await api.getTranscript(jobId);
      setEditor({
        segments: tr.segments.map((s) => ({ start: s.start, end: s.end, text: s.text })),
        translations: tr.translations ?? [],
      });
    } catch (e) {
      setLoadError(String(e));
      setEditor(null);
    }
  }, [jobId]);

  useEffect(() => {
    audioRef.current?.pause();
    setPlayingIndex(null);
    setEditor(null);
    loadTranscript();
  }, [jobId]);

  const togglePlay = useCallback((i: number) => {
    const a = audioRef.current;
    if (!a || !editor) return;
    if (playingIndex === i) {
      a.pause();
      setPlayingIndex(null);
      return;
    }
    const seg = editor.segments[i];
    if (!seg) return;
    a.currentTime = seg.start;
    void a.play();
    setPlayingIndex(i);
  }, [editor, playingIndex]);

  const onTimeUpdate = useCallback(() => {
    const a = audioRef.current;
    if (!a || playingIndex === null || !editor) return;
    const seg = editor.segments[playingIndex];
    if (seg && a.currentTime >= seg.end - 0.02) {
      a.pause();
      setPlayingIndex(null);
    }
  }, [editor, playingIndex]);

  const saveEdits = useCallback(async () => {
    if (!editor) return;
    try {
      await api.updateTranscript(
        jobId,
        editor.segments,
        editor.translations.length ? editor.translations : undefined
      );
      await loadTranscript();
    } catch (e) {
      setLoadError(String(e));
    }
  }, [editor, jobId]);

  const rowOp = useCallback(
    async (op: "merge_next" | "split" | "delete", i: number) => {
      if (!editor) return;
      try {
        await api.updateTranscript(
          jobId,
          editor.segments,
          editor.translations.length ? editor.translations : undefined
        );
        await api.rowOp(jobId, i, op);
        await loadTranscript();
      } catch (e) {
        setLoadError(String(e));
      }
    },
    [editor, jobId]
  );

  const updSeg = (i: number, patch: Partial<EditableSegment>) => {
    setEditor((ed) =>
      ed
        ? { ...ed, segments: ed.segments.map((s, j) => (j === i ? { ...s, ...patch } : s)) }
        : ed
    );
  };
  const updTr = (i: number, v: string) => {
    setEditor((ed) =>
      ed
        ? { ...ed, translations: ed.translations.map((t, j) => (j === i ? v : t)) }
        : ed
    );
  };

  const doReexport = useCallback(async () => {
    try {
      await api.reexport(jobId, {
        variant: reexportVariant,
        save_to_video_folder: reexportSave,
        embed_video: reexportEmbed,
        output_dir: reexportDir.trim() || undefined,
      });
    } catch (e) {
      setLoadError(String(e));
    }
  }, [jobId, reexportVariant, reexportSave, reexportEmbed, reexportDir]);

  const stopJob = useCallback(async () => {
    if (!window.confirm("确定停止该任务？已完成的步骤产物会保留。")) return;
    try {
      await api.cancelJob(jobId);
    } catch (e) {
      setLoadError(String(e));
    }
  }, [jobId]);

  const fileName = job ? job.video_path.split("/").pop() : jobId;
  const exportStep = job?.steps?.export;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="row">
          <Tooltip side="bottom" text="返回任务列表。表格里尚未保存的修改会丢失。">
            <button style={{ padding: "4px 12px" }} onClick={onBack}>← 返回列表</button>
          </Tooltip>
          <h2 style={{ margin: 0 }}>{fileName}</h2>
          <span className="muted">#{jobId}</span>
        </div>
        <div className="row">
          {job && (job.status === "running" || job.status === "queued") && (
            <Tooltip
              align="right"
              side="bottom"
              text="停止该任务。识别会在当前这段音频处理完后中断；已经完成的步骤产物都会保留。"
            >
              <button className="ghost" onClick={stopJob}>停止任务</button>
            </Tooltip>
          )}
          {job?.backend && "name" in job.backend && (
            <span className="step">{job.backend.name}/{job.backend.device}</span>
          )}
        </div>
      </div>

      <div className="panel" style={{ marginTop: 12 }}>
        <div className="row">
          <b>导出：</b>
          <Tooltip side="bottom" text="选择要导出的字幕形式：双语对照 / 仅译文 / 仅原文。">
            <label className="muted">字幕类型</label>
          </Tooltip>
          <Tooltip side="bottom" text="双语对照 = 原文一行 + 译文一行；仅译文只保留翻译结果。">
            <select value={reexportVariant} onChange={(e) => setReexportVariant(e.target.value)}>
              <option value="bilingual">双语对照</option>
              <option value="target">仅译文</option>
              <option value="source">仅原文</option>
            </select>
          </Tooltip>
          <Tooltip side="bottom" text="勾选后会把字幕文件复制一份到输出目录。不勾则不落任何字幕文件，改动只保留在任务内部。">
            <label className="row" style={{ gap: 4 }}>
              <input
                type="checkbox"
                style={{ width: "auto" }}
                checked={reexportSave}
                onChange={(e) => setReexportSave(e.target.checked)}
              />
              <span className="muted">保存字幕文件</span>
            </label>
          </Tooltip>
          <Tooltip side="bottom" text="用 ffmpeg 把字幕烧进画面生成新视频。需要重新编码，比较耗时。它与「保存字幕文件」互不影响。">
            <label className="row" style={{ gap: 4 }}>
              <input
                type="checkbox"
                style={{ width: "auto" }}
                checked={reexportEmbed}
                onChange={(e) => setReexportEmbed(e.target.checked)}
              />
              <span className="muted">烧录硬字幕视频</span>
            </label>
          </Tooltip>
          <Tooltip side="bottom" text="按表格里的当前内容重新生成字幕文件（以及可选的硬字幕视频）。改动请先点下方的「保存修改」。两个选项都不勾时不会往输出目录写任何文件。">
            <button
              onClick={doReexport}
              disabled={!editor || exportStep?.status === "running"}
            >
              重新导出
            </button>
          </Tooltip>
          {exportStep && exportStep.status !== "pending" && (
            <span className={`step ${exportStep.status}`}>
              {STEP_LABELS.export}·{exportStep.status}
              {exportStep.status === "running" ? ` ${Math.round(exportStep.progress * 100)}%` : ""}
            </span>
          )}
          {exportStep?.detail && <span className="muted">{exportStep.detail}</span>}
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <Tooltip side="bottom" text="字幕文件和硬字幕视频的保存位置。留空则保存到原视频所在文件夹。">
            <label className="muted">输出目录</label>
          </Tooltip>
          <input
            type="text"
            value={reexportDir}
            placeholder="留空 = 原视频所在文件夹"
            onChange={(e) => setReexportDir(e.target.value)}
          />
          <button className="ghost" onClick={() => setPickDir(true)}>
            浏览…
          </button>
          {reexportDir && (
            <button className="ghost" onClick={() => setReexportDir("")}>
              恢复默认
            </button>
          )}
        </div>
      </div>

      <h2>字幕编辑器</h2>
      <div className="panel">
        {loadError && <div className="error">{loadError}</div>}
        {!editor && !loadError && <span className="muted">加载中…</span>}
        {editor && (
          <>
            <audio
              ref={(el) => { audioRef.current = el; }}
              src={ENGINE_URL + "/jobs/" + jobId + "/audio"}
              preload="auto"
              style={{ display: "none" }}
              onTimeUpdate={onTimeUpdate}
            />
            <table>
              <thead>
                <tr><th>播放</th><th>开始</th><th>结束</th><th>原文</th><th>译文</th><th>操作</th></tr>
              </thead>
              <tbody>
                {editor.segments.map((s, i) => (
                  <tr key={i}>
                    <td>
                      <Tooltip side="bottom" text={playingIndex === i ? "停止播放" : "播放这一句对应的原声"}>
                        <button
                          style={{ padding: "2px 8px" }}
                          onClick={() => togglePlay(i)}
                        >
                          {playingIndex === i ? "⏹" : "▶"}
                        </button>
                      </Tooltip>
                    </td>
                    <td>
                      <input
                        type="number"
                        step="0.01"
                        style={{ width: 78 }}
                        value={s.start}
                        onChange={(e) => updSeg(i, { start: Number(e.target.value) })}
                      />
                    </td>
                    <td>
                      <input
                        type="number"
                        step="0.01"
                        style={{ width: 78 }}
                        value={s.end}
                        onChange={(e) => updSeg(i, { end: Number(e.target.value) })}
                      />
                    </td>
                    <td style={{ minWidth: 220 }}>
                      <input
                        type="text"
                        style={{ width: "100%" }}
                        value={s.text}
                        onChange={(e) => updSeg(i, { text: e.target.value })}
                      />
                    </td>
                    <td style={{ minWidth: 220 }}>
                      <input
                        type="text"
                        style={{ width: "100%" }}
                        value={editor.translations[i] ?? ""}
                        onChange={(e) => updTr(i, e.target.value)}
                      />
                    </td>
                    <td>
                      <Tooltip side="bottom" text="在这一句的中间位置切成两条字幕。">
                        <button style={{ padding: "2px 6px" }} onClick={() => rowOp("split", i)}>拆</button>
                      </Tooltip>{" "}
                      <Tooltip side="bottom" text="把这一条与下一条合并成一条。">
                        <button style={{ padding: "2px 6px" }} onClick={() => rowOp("merge_next", i)}>并</button>
                      </Tooltip>{" "}
                      <Tooltip align="right" side="bottom" text="删除这一条字幕。">
                        <button style={{ padding: "2px 6px" }} onClick={() => rowOp("delete", i)}>删</button>
                      </Tooltip>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="row" style={{ marginTop: 10 }}>
              <Tooltip side="bottom" text="把表格里的文本与时间轴改动写回任务。必须先保存，再点上方「重新导出」才会生效到字幕文件或视频。">
                <button onClick={saveEdits}>保存修改</button>
              </Tooltip>
              <span className="muted">修改后先保存，再点上方「重新导出」生效到字幕文件 / 视频。</span>
            </div>
          </>
        )}
      </div>

      {pickDir && (
        <DirPicker
          value={reexportDir || undefined}
          onPick={setReexportDir}
          onClose={() => setPickDir(false)}
        />
      )}
    </>
  );
}
