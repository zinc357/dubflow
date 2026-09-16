import { useCallback, useEffect, useState } from "react";
import { api, BrowseResult, FileEntry } from "../api";

interface Props {
  /** 打开时的起始目录 */
  initialPath?: string;
  onPick: (path: string) => void;
  onClose: () => void;
}

const KIND_LABEL: Record<string, string> = {
  video: "选择视频文件",
};

/**
 * 视频文件选择器。
 *
 * 与 DirPicker 相同的安全模型：目录/文件枚举由引擎 /fs/browse 完成，
 * 前端只负责展示与点选 —— 浏览器与 Tauri 桌面窗口行为一致。
 */
export default function FilePicker({ initialPath, onPick, onClose }: Props) {
  const [listing, setListing] = useState<BrowseResult | null>(null);
  const [manual, setManual] = useState(initialPath ?? "");
  const [err, setErr] = useState("");

  const load = useCallback(async (path?: string) => {
    setErr("");
    try {
      const d = await api.browse(path || undefined);
      setListing(d);
      setManual(d.path);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    void load(initialPath || undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pickFile = (f: FileEntry) => {
    onPick(f.path);
    onClose();
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{KIND_LABEL.video ?? "选择文件"}</h3>

        <div className="row">
          <input
            type="text"
            value={manual}
            placeholder="目录绝对路径"
            onChange={(e) => setManual(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void load(manual);
            }}
          />
          <button onClick={() => void load(manual)}>前往</button>
        </div>

        {listing && listing.drives.length > 0 && (
          <div className="row" style={{ marginTop: 8 }}>
            {listing.drives.map((d) => (
              <button key={d} style={{ padding: "2px 10px" }} onClick={() => void load(d)}>
                {d}
              </button>
            ))}
          </div>
        )}

        {err && <div className="error">{err}</div>}

        {listing && (
          <div style={{ marginTop: 10, maxHeight: 320, overflowY: "auto" }}>
            {listing.parent && (
              <div className="dir-row" style={{ padding: "3px 0" }}>
                <span className="muted">↩ 上级目录</span>
                <button
                  style={{ marginLeft: "auto", padding: "2px 10px" }}
                  onClick={() => void load(listing.parent!)}
                >
                  打开
                </button>
              </div>
            )}
            {listing.dirs.map((d) => (
              <div key={d.path} className="dir-row" style={{ padding: "3px 0" }}>
                <span>📁 {d.name}</span>
                <button
                  style={{ marginLeft: "auto", padding: "2px 10px" }}
                  onClick={() => void load(d.path)}
                >
                  打开
                </button>
              </div>
            ))}
            {listing.files.map((f) => (
              <div key={f.path} className="dir-row" style={{ padding: "3px 0" }}>
                <span>🎬 {f.name}</span>
                <button style={{ marginLeft: "auto", padding: "2px 10px" }} onClick={() => pickFile(f)}>
                  选择
                </button>
              </div>
            ))}
            {listing.dirs.length === 0 && listing.files.length === 0 && (
              <span className="muted">此目录为空</span>
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
