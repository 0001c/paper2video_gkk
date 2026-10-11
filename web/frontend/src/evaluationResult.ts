export type AnswerResult = { predicted: string | null; answer: string; correct: boolean; supported: boolean; evidence: string };

export function answerResult(item: AnswerResult) {
  if (item.correct) return { label: '✓ 已计分', explanation: '选项正确，材料依据已通过本轮核验。' };
  if (item.predicted == null) return { label: '未作答', explanation: '模型未给出选项，未计分。请核对材料是否覆盖此题。' };
  if (item.predicted !== item.answer) return { label: '选项错误', explanation: '模型选择与题库正确答案不同，未计分。' };
  return item.evidence ? { label: '选项正确 · 依据待核验', explanation: '选项一致，但引文未通过本轮材料匹配，因此未计分。请查看引文，区分材料缺失与引文格式问题。' }
    : { label: '选项正确 · 缺少依据', explanation: '选项一致，但没有提供材料依据，因此未计分。' };
}
