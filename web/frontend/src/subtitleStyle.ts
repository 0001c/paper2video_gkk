export type SubtitleStyle = {
  enabled: boolean; font_name: string; font_size: number; text_color: string;
  background_color: string; background_opacity: number; margin_horizontal: number; margin_bottom: number; line_spacing: number;
};

export const defaultSubtitleStyle: SubtitleStyle = {
  enabled: true, font_name: "", font_size: 32, text_color: "#FFFFFF",
  background_color: "#000000", background_opacity: 0.55, margin_horizontal: 5, margin_bottom: 3, line_spacing: 1.6,
};

export function readSubtitleStyle(value: unknown): SubtitleStyle | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const raw = value as Record<string, unknown>;
  if (Object.keys(raw).some((key) => !Object.keys(defaultSubtitleStyle).includes(key))) return;
  const style = { ...defaultSubtitleStyle, ...raw };
  if (typeof style.enabled !== "boolean" || typeof style.font_name !== "string"
    || style.font_name.length > 80 || /[\x00-\x1f,\\{}]/.test(style.font_name)) return;
  // 清除旧配置中误填的接口地址，保留用户的字号、颜色和边距。
  if (/\/|^[a-z][a-z0-9+.-]*:/i.test(style.font_name.trim())) style.font_name = "";
  for (const key of ["text_color", "background_color"] as const) {
    if (typeof style[key] !== "string" || !/^#[0-9a-f]{6}$/i.test(style[key])) return;
  }
  for (const [key, min, max] of [["font_size", 16, 72], ["background_opacity", 0, 1],
    ["margin_horizontal", 2, 20], ["margin_bottom", 1, 15], ["line_spacing", 1.6, 2.4]] as const) {
    if (typeof style[key] !== "number" || !Number.isFinite(style[key]) || style[key] < min || style[key] > max) return;
  }
  if (!Number.isInteger(style.font_size)) return;
  return { ...style, font_name: style.font_name.trim(), text_color: style.text_color.toUpperCase(), background_color: style.background_color.toUpperCase() };
}

export function subtitleBackground(style: SubtitleStyle) {
  const color = style.background_color;
  return `rgba(${parseInt(color.slice(1, 3), 16)}, ${parseInt(color.slice(3, 5), 16)}, ${parseInt(color.slice(5, 7), 16)}, ${style.background_opacity})`;
}
