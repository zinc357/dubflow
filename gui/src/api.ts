export const ENGINE_URL = "http://127.0.0.1:8741";

export interface BackendInfo {
  name: string;
  device: string;
  detail: string;
}

export interface Health {
  status: string;
  version: string;
  backend: BackendInfo;
  data_dir: string;
}

export interface StepStatus {
  status: "pending" | "queued" | "running" | "done" | "failed" | "skipped";
  progress: number;
  detail: string;
}

export interface Job {
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  video_path: string;
  source_language: string | null;
  target_language: string;
  steps: Record<string, StepStatus>;
  error: string | null;
  artifacts: Record<string, string>;
  backend: BackendInfo | Record<string, never>;
  created_at?: number;
  // 引擎回显的导出选择，工作台用它作为「重新导出」的默认值
  export_options?: {
    variant?: string;
    save_to_video_folder?: boolean;
    embed_video?: boolean;
    output_dir?: string | null;
  };
}


export interface DownloadState {
  status: string;
  progress: number;
  detail: string;
}

export interface ModelInfo extends DownloadState {
  key: string;
  repo: string;
  backend: string;
  downloaded: boolean;
  size_mb: number;
}

export interface DownloadsSnapshot {
  ffmpeg: DownloadState & { installed: boolean; ffmpeg: string | null };
  models: ModelInfo[];
}

export interface Segment {
  start: number;
  end: number;
  text: string;
}

export interface EditableSegment {
  start: number;
  end: number;
  text: string;
}

export interface DirEntry {
  name: string;
  path: string;
  writable: boolean;
}

export interface DirListing {
  path: string;
  parent: string | null;
  dirs: DirEntry[];
  drives: string[];
  is_writable: boolean;
}

// 引擎侧持久化的翻译配置。密钥只回掩码，不回明文：
// 表单把密钥框留空即表示「沿用已保存的值」，由引擎的 settings 兜底。
export interface LlmSettings {
  base_url: string;
  model: string;
  api_key_set: boolean;
  api_key_hint: string;
}

export interface MsftSettings {
  region: string;
  key_set: boolean;
  key_hint: string;
}

export interface EngineSettings {
  llm: LlmSettings;
  microsoft: MsftSettings;
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(ENGINE_URL + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => req<Health>("GET", "/health"),
  listJobs: () => req<{ jobs: Job[] }>("GET", "/jobs"),
  getJob: (id: string) => req<Job>("GET", `/jobs/${id}`),
  createJob: (payload: {
    video_path: string;
    source_language: string | null;
    target_language: string;
    asr: { provider: string; model: string | null };
    translation: {
      enabled: boolean;
      provider: string;
      base_url?: string;
      api_key?: string;
      region?: string;
      model?: string;
    };
    export: {
      variant: string;
      save_to_video_folder: boolean;
      embed_video: boolean;
      output_dir?: string | null;
    };
    }) => req<Job>("POST", "/jobs", payload),
  getTranscript: (id: string) =>
    req<{
      language: string | null;
      segments: Segment[];
      translations: string[] | null;
      target_language?: string;
    }>("GET", `/jobs/${id}/transcript`),
  updateTranscript: (
    id: string,
    segments: EditableSegment[],
    translations?: string[]
  ) =>
    req<{ ok: boolean; segments: number }>("PUT", `/jobs/${id}/transcript`, {
      segments,
      translations,
    }),
  rowOp: (id: string, index: number, op: "merge_next" | "split" | "delete") =>
    req<{ ok: boolean }>("POST", `/jobs/${id}/segments/${index}/${op}`),
  reexport: (
    id: string,
    overrides: {
      variant?: string;
      save_to_video_folder?: boolean;
      embed_video?: boolean;
      output_dir?: string;
    }
  ) => req<{ ok: boolean }>("POST", `/jobs/${id}/export`, overrides),
  cancelJob: (id: string) => req<{ ok: boolean }>("POST", `/jobs/${id}/cancel`),
  listDirs: (path?: string) =>
    req<DirListing>("GET", `/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  getSettings: () => req<EngineSettings>("GET", "/settings"),
  putSettings: (payload: {
    llm?: { base_url?: string; model?: string; api_key?: string };
    microsoft?: { key?: string; region?: string };
  }) => req<EngineSettings>("PUT", "/settings", payload),
  downloads: () => req<DownloadsSnapshot>("GET", "/downloads"),
  downloadModel: (key: string) =>
    req<{ ok: boolean }>("POST", `/downloads/models/${key}`),
  downloadFfmpeg: () => req<{ ok: boolean }>("POST", "/downloads/ffmpeg"),
  deleteJob: (id: string) => req<{ ok: boolean }>("DELETE", `/jobs/${id}`),
  clearFailed: () => req<{ ok: boolean; removed: number }>("POST", "/jobs/clear-failed"),
};
