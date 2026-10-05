export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请重试";
}

async function request(path: string, init: RequestInit = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch(path, { cache: "no-store", ...init, signal: controller.signal });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/api/auth/') && typeof window !== 'undefined') {
        window.dispatchEvent(new Event('paper2video:auth-expired'));
      }
      const detail = data?.detail || data?.error;
      const problems = detail?.problems ?? (Array.isArray(detail) ? detail.map((item: any) => ({ field: item.loc?.join("."), message: item.msg })) : []);
      throw new Error(typeof detail === "string" ? detail : problems.length
        ? problems.map((item: any) => `${item.field || "配置"}：${item.message}`).join("；") : `请求失败（HTTP ${response.status}）`);
    }
    if (data === null) throw new Error("服务返回了无法读取的响应，请检查服务后重试");
    return { status: response.status, data };
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("请求超时。创建或续跑任务时，请先检查任务记录再重试，避免重复创建");
    }
    if (error instanceof TypeError) throw new Error("无法连接服务，请检查服务是否运行后重试");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  patch: (path: string, body: unknown) => request(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  delete: (path: string) => request(path, { method: "DELETE" }),
  put: (path: string, body: unknown) => request(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  get: (path: string) => request(path).then((r) => r.data),
  post: (path: string, body?: unknown, headers?: Record<string, string>) => request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  }),
  postForm: (path: string, form: FormData) => request(path, { method: "POST", body: form }),
};

// ref 同步加锁，避免 React 下一次渲染前的第二次点击；所有失败路径都释放加载态。
export async function runExclusive(
  lock: { current: boolean },
  busy: (value: boolean) => void,
  action: () => Promise<unknown>,
  onError: (error: unknown) => void,
) {
  if (lock.current) return;
  lock.current = true;
  busy(true);
  try { await action(); }
  catch (error) { onError(error); }
  finally { lock.current = false; busy(false); }
}

export function applyTemplate(current: string, previous: string, next: string): string {
  return !current || current === previous ? next : current;
}
