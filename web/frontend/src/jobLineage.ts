export type JobOrigin = { id: string; resumed_from?: string; created_at?: string };
export type JobHistoryNode<T extends JobOrigin> = { job: T; children: JobHistoryNode<T>[]; unavailableParent?: string };

/** 接口按创建时间倒序返回；分组按最近活动排序，组内按生成先后展示。 */
export function groupJobHistory<T extends JobOrigin>(jobs: T[]): JobHistoryNode<T>[] {
  const nodes = new Map(jobs.map(job => [job.id, { job, children: [] } as JobHistoryNode<T>]));
  const order = new Map(jobs.map((job, index) => [job.id, index]));
  const parents = new Map<string, string>();
  for (const { job } of nodes.values()) {
    if (job.resumed_from && job.resumed_from !== job.id && nodes.has(job.resumed_from)) parents.set(job.id, job.resumed_from);
  }
  // 旧数据缺少来源或出现循环时保留任务，不能让整组从记录页消失。
  for (const { job } of nodes.values()) {
    const seen = new Set([job.id]);
    let parent = parents.get(job.id);
    while (parent) {
      if (seen.has(parent)) { parents.delete(job.id); break; }
      seen.add(parent);
      parent = parents.get(parent);
    }
  }
  const roots: JobHistoryNode<T>[] = [];
  for (const node of nodes.values()) {
    const parent = parents.get(node.job.id);
    if (parent) nodes.get(parent)!.children.push(node);
    else {
      if (node.job.resumed_from) node.unavailableParent = node.job.resumed_from;
      roots.push(node);
    }
  }
  const latest = (node: JobHistoryNode<T>): number => Math.min(order.get(node.job.id)!, ...node.children.map(latest));
  const sortChildren = (node: JobHistoryNode<T>) => {
    node.children.sort((a, b) => (a.job.created_at ?? '').localeCompare(b.job.created_at ?? '') || order.get(b.job.id)! - order.get(a.job.id)!);
    node.children.forEach(sortChildren);
  };
  roots.forEach(sortChildren);
  return roots.sort((a, b) => latest(a) - latest(b));
}

export function jobDescendants<T extends JobOrigin>(node: JobHistoryNode<T>, depth = 1): { node: JobHistoryNode<T>; depth: number }[] {
  return node.children.flatMap(child => [{ node: child, depth }, ...jobDescendants(child, depth + 1)]);
}
