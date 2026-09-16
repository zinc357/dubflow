/**
 * 表单偏好的本地记忆。
 *
 * 只放「跟着这个浏览器走」的界面选项。
 * 翻译配置（API 地址 / 模型 / **API Key**）刻意不放这里 ——
 * 那部分由引擎持久化到 ~/.dubflow/settings.json，见 api.getSettings()。
 */

export interface FormPrefs {
  sourceLang: string;
  targetLang: string;
  model: string;
  translate: boolean;
  trProvider: string;
  subtitleVariant: string;
  saveSrt: boolean;
  embedVideo: boolean;
  outputDir: string;
}

const STORAGE_KEY = "dubflow.formPrefs";

export function loadFormPrefs(): Partial<FormPrefs> {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as Partial<FormPrefs>;
  } catch {
    // 隐私模式、存储被禁用、内容被改坏 —— 一律当作「没有偏好」
    return {};
  }
}

export function saveFormPrefs(prefs: FormPrefs): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // 存不下也不该影响功能
  }
}
