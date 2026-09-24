import { useCallback, useEffect, useState } from "react";
import { api, BrowseResult, FileEntry } from "../api";

interface Props {
  /** 打开时的起始目录 */
  initialPath?: string;
  onPick: (path: string) => void;
  onClose: () => void;
}

const LAST_DIR_KEY = "dubflow.lastBrowseDir";

function fmtSize(mb: number): string {
  if (mb >= 1024) return (mb / 1024).toFixed(2) + " GB";
  if (mb >= 1) return mb.toFixed(1) + " MB";
  return Math.max(1, Math.round(mb * 1024)) + " KB";
}

function joinPath(base: string, name: string): string {
  return base.endsWith("\\") || base.endsWith("/") ? base + name : base + "\\" + name;
}

function breadcrumbs(path: string): { label: string; path: string }[] {
  const isWin = path.includes("\\");
  const sep = isWin ? "\\" : "/";
  const parts = path.split(sep).filter(Boolean);
  const out: { label: string; path: string }[] = [];
  let acc = "";
  parts.forEach((p, idx) => {
    if (isWin && idx === 0) {
      acc = p; // "C:"
      out.push({ label: p, path: acc });
    } else {
      acc = acc.endsWith(sep) ? acc + p : acc + sep + p;
      out.push({ label: p, path: acc });
    }
  });
  return out;
}

const chipStyle: React.CSSProperties = {
  padding: "3px 10px", border: "1px solid #ddd", borderRadius: 14,
  background: "#f5f6f8", cursor: "pointer", fontSize: 12,
};

/**
 * 视频文件选择器。
 *
 * 目录/文件枚举由引擎 /fs/browse 完成，前端只负责展示与点选 —— 浏览器
 * 与 Tauri 桌面窗口行为一致。支持快捷位置、面包屑导航、双击进入/选择、
 * 记住上次浏览目录。
 */
export default function FilePicker({ initialPath, onPick, onClose }: Props) {
  const [listing, setListing] = useState<BrowseResult | null>(null);
  const [manual, setManual] = useState(initialPath ?? "");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [home, setHome] = useState("");

  const load = useCallback(async (path?: string, remember = true) => {
    setErr(""); setLoading(true);
    try {
      const d = await api.browse(path || undefined);
      setListing(d);
      setManual(d.path);
      if (!home) setHome(d.path);
      if (remember) localStorage.setItem(LAST_DIR_KEY, d.path);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [home]);

  useEffect(() => {
    const last = localStorage.getItem(LAST_DIR_KEY) || "";
    void load(initialPath || last || undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pickFile = (f: FileEntry) => {
    onPick(f.path);
    onClose();
  };

  const quick = home ? [
    { label: "🏠 主目录", path: home },
    { label: "🖥️ 桌面", path: joinPath(home, "Desktop") },
    { label: "📥 下载", path: joinPath(home, "Downloads") },
    { label: "📄 文档", path: joinPath(home, "Documents") },
    { label: "🎬 视频", path: joinPath(home, "Videos") },
  ] : [];

  const crumbs = listing ? breadcrumbs(listing.path) : [];

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>选择视频文件</h3>

        {/* 快捷位置 */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
          {quick.map((q) => (
            <button key={q.label} style={chipStyle} onClick={() => void load(q.path)}>
              {q.label}
            </button>
          ))}
          {listing && listing.drives.length > 0 && (
            <select
              style={{ ...chipStyle, width: 90 }}
              value=""
              onChange={(e) => { if (e.target.value) void load(e.target.value); }}
            >
              <option value="">💾 驱动器…</option>
              {listing.drives.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          )}
        </div>

        {/* 面包屑导航 */}
        {crumbs.length > 0 && (
          <div style={{ marginBottom: 8, fontSize: 12, display: "flex", flexWrap: "wrap", gap: 2 }}>
            {crumbs.map((c, idx) => (
              <span key={c.path} style={{ display: "inline-flex", alignItems: "center" }}>
                {idx > 0 && <span style={{ color: "#999", margin: "0 2px" }}>›</span>}
                <button
                  style={{ border: "none", background: "none", color: idx === crumbs.length - 1 ? "#333" : "#4f8cff", cursor: "pointer", fontSize: 12, padding: "2px 3px" }}
                  onClick={() => void load(c.path)}
                >
                  {c.label}
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="row">
          <input
            type="text"
            value={manual}
            placeholder="输入目录绝对路径后回车"
            onChange={(e) => setManual(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void load(manual); }}
          />
          <button onClick={() => void load(manual)}>前往</button>
        </div>

        {err && <div className="error">{err}</div>}
        {loading && <div style={{ color: "#888", fontSize: 12, marginTop: 6 }}>加载中…</div>}

        {listing && !loading && (
          <div style={{ marginTop: 10, maxHeight: 340, overflowY: "auto", border: "1px solid #eee", borderRadius: 6 }}>
            {listing.parent && (
              <div
                className="dir-row"
                style={{ padding: "6px 10px", cursor: "pointer", borderBottom: "1px solid #f0f0f0" }}
                onClick={() => void load(listing.parent!)}
              >
                <span className="muted">↩ 上级目录</span>
              </div>
            )}
            {listing.dirs.map((d) => (
              <div
                key={d.path}
                className="dir-row"
                style={{ padding: "6px 10px", cursor: "pointer", borderBottom: "1px solid #f5f5f5" }}
                onClick={() => void load(d.path)}
                onDoubleClick={() => void load(d.path)}
              >
                <span>📁 {d.name}</span>
                <span style={{ marginLeft: "auto", color: "#4f8cff", fontSize: 12 }}>打开 ›</span>
              </div>
            ))}
            {listing.files.map((f) => (
              <div
                key={f.path}
                className="dir-row"
                style={{ padding: "6px 10px", cursor: "pointer", borderBottom: "1px solid #f5f5f5" }}
                onClick={() => void 0}
                onDoubleClick={() => pickFile(f)}
              >
                <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  🎬 {f.name}
                </span>
                <span style={{ color: "#888", fontSize: 12, marginRight: 10, whiteSpace: "nowrap" }}>
                  {fmtSize(f.size_mb)}
                </span>
                <button style={{ padding: "2px 10px", whiteSpace: "nowrap" }} onClick={() => pickFile(f)}>
                  选择
                </button>
              </div>
            ))}
            {listing.dirs.length === 0 && listing.files.length === 0 && (
              <div style={{ padding: 14, textAlign: "center" }} className="muted">此目录为空</div>
            )}
          </div>
        )}

        <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
          <button onClick={onClose}>取消</button>
        </div>
      </div>
    </div>
  );
}
