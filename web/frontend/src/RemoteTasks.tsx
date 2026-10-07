import { useEffect, useRef, useState } from "react";
import { Button } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";

export type RemoteTaskDetails = {
  items: { record: string; model: string; task_id: string | null; request_id: string | null; state: string;
    remote_status: string | null; resolution: string | null; cancellation_confirmed: boolean; archived?: boolean; needs_manual_review: boolean }[];
  known_ids: string[]; needs_manual_review: boolean; cancel_state: string; warning?: string | null;
  errors?: { message: string }[];
};
const stateLabels: Record<string, string> = { submitted: "已提交", pending: "排队中", running: "生成中", succeeded: "云端生成成功", failed: "失败",
  canceled: "云端已确认取消", cancel_unconfirmed: "取消未确认", submission_unknown: "提交结果不明", needs_manual_review: "需要人工核对", cancelled_before_submission: "提交前已停止", query_failed: "续查失败，可再次对账", download_failed: "生成成功，归档待重试", poll_timeout: "轮询超时，可继续对账", remote_unknown: "云端任务未知，需要人工核对" };

export function RemoteTasks({ jobId, status, details }: { jobId: string; status: string; details?: RemoteTaskDetails }) {
  const [current, setCurrent] = useState(details);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  useEffect(() => { setCurrent(details); setMessage(""); }, [jobId, details]);
  const running = status === "queued" || status === "running";
  const operate = (action: "reconcile" | "cancel") => runExclusive(lock, setBusy, async () => {
    setMessage("");
    const { data } = await api.post(action === "reconcile" ? `/api/jobs/${jobId}/remote-tasks/reconcile` : `/api/jobs/${jobId}/cancel`);
    const next: RemoteTaskDetails = action === "reconcile" ? data : data.remote_tasks;
    setCurrent(next);
    const archived = next.items.filter((item) => item.archived).length;
    setMessage(data.remote_error ?? data.warning ?? (action === "cancel"
      ? data.remote_cancel_state === "cancel_confirmed" ? "云端取消已确认；实际已发生费用以百炼账单为准。" : "已保存取消意图，云端取消尚未确认。"
      : `本次对账：${next.known_ids.length}个已知远程ID，${archived}个结果已归档。${next.needs_manual_review ? "提交结果不明的片段仍需在百炼控制台人工核对。" : ""}未上传或提交新任务。`));
  }, (error) => setMessage(errorMessage(error)));
  return <details className="task-model-settings"><summary>Wan云端任务与费用状态{current?.known_ids.length ? ` · ${current.known_ids.length}个已知ID` : ""}</summary>
    <p className="hint">停止本地进程后，云端任务仍可能继续处理或计费。服务重启后先用下方连接恢复入口重绑原账号范围，再查询已知ID。</p>
    {current?.warning && <p className="notice warn" role="status">{current.warning}</p>}
    {current?.needs_manual_review && <p className="notice warn">存在缺少远程ID、提交结果不明或远程状态无法确认的片段，需要在百炼控制台人工核对，禁止直接重复提交。</p>}
    {current?.items.length ? <dl className="model-snapshot">{current.items.map((item) => <div key={item.record}><dt>{item.model} · {item.resolution ?? "分辨率未记录"}</dt><dd>
      {stateLabels[item.state] ?? item.state} · {item.remote_status ?? "远程状态未确认"}
      <span className="hint">远程任务ID：{item.task_id ?? (item.state === "cancelled_before_submission" ? "提交前已停止，无远程任务ID" : "缺失，需要人工核对")}</span>
      <span className="hint">请求ID：{item.request_id ?? "未记录"}</span>
      {item.archived && <span className="hint">结果已下载归档。</span>}
      {item.cancellation_confirmed && <span className="hint">{item.state === "cancelled_before_submission" ? "提交前已停止，未提交生成任务。" : "已确认云端取消；实际已发生费用以百炼账单为准。"}</span>}
    </dd></div>)}</dl> : <p className="hint">尚无已持久化的Wan远程任务记录。</p>}
    <div className="actions">
      <Button variant="outline" disabled={running || busy} loading={busy} onClick={() => operate("reconcile")}>查询并归档已有云端结果</Button>
      {!!current?.known_ids.length && current.cancel_state !== "cancel_confirmed" && <Button theme="danger" variant="text" disabled={running || busy} onClick={() => operate("cancel")}>再次请求云端取消</Button>}
    </div>
    {running && <p className="hint">本地任务运行或排队中，请先使用“取消此任务”停止新请求，再进行远程对账。</p>}
    {current?.errors?.map((error, index) => <p className="notice warn" key={index}>{error.message}</p>)}
    {message && <p className="notice info" role="status">{message}</p>}
  </details>;
}
