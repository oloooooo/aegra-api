import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ breaks: true, gfm: true });

/** 模型输出是 Markdown（常含表格），渲染后消毒；不带原始 HTML 信任。 */
export function renderMarkdown(text: string): string {
  const html = marked.parse(text ?? "", { async: false }) as string;
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}
