import { defineComponent, h, type PropType, type VNodeArrayChildren, type VNodeChild } from "vue";
import { Lexer, type Token, type Tokens } from "marked";

import { safeUrl } from "../../utils/safeUrl";

// Upstream release notes are untrusted. Markdown is parsed into tokens and rendered
// as Vue nodes, so raw HTML is never injected: tags are reduced to their text,
// images become links, and only absolute http(s) links stay clickable.

type HeadingTag = "h3" | "h4" | "h5" | "h6";

const NAMED_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  ndash: "–",
  mdash: "—",
  hellip: "…",
  lsquo: "‘",
  rsquo: "’",
  ldquo: "“",
  rdquo: "”",
  laquo: "«",
  raquo: "»",
  middot: "·",
  bull: "•",
  larr: "←",
  rarr: "→",
  times: "×",
  deg: "°",
  copy: "©",
  reg: "®",
  trade: "™",
};

// Token text keeps entity references for an HTML renderer to resolve, so decode numeric
// and common named ones here; other names stay literal. Code spans and autolinks are literal.
function decodeEntities(value: string): string {
  return value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (match, entity: string) => {
    if (entity[0] !== "#") return NAMED_ENTITIES[entity.toLowerCase()] ?? match;
    const code = entity[1].toLowerCase() === "x"
      ? Number.parseInt(entity.slice(2), 16)
      : Number.parseInt(entity.slice(1), 10);
    return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : match;
  });
}

function htmlText(raw: string): string {
  return decodeEntities(raw.replace(/<!--[\s\S]*?(?:-->|$)/g, "").replace(/<[^>]*>/g, ""));
}

function link(token: Tokens.Generic, children: string | VNodeArrayChildren, inLink: boolean): VNodeChild {
  // Autolinked URLs are literal; other destinations may carry entity references.
  const href = token.autolink ? token.href : decodeEntities(token.href);
  if (inLink || !safeUrl(href)) return children;
  return h("a", { class: "text-link", href, target: "_blank", rel: "noopener noreferrer" }, children);
}

function renderInline(tokens: Token[] | undefined, inLink = false): VNodeChild[] {
  return (tokens ?? []).map((token) => {
    const t = token as Tokens.Generic;
    switch (t.type) {
      case "strong":
        return h("strong", renderInline(t.tokens, inLink));
      case "em":
        return h("em", renderInline(t.tokens, inLink));
      case "del":
        return h("del", renderInline(t.tokens, inLink));
      case "codespan":
        return h("code", t.text);
      case "br":
        return h("br");
      case "link":
        return link(t, t.autolink ? t.text : renderInline(t.tokens, true), inLink);
      case "image": {
        const label = t.tokens?.length ? renderInline(t.tokens, true) : ["image"];
        return link(t, ["[", ...label, "]"], inLink);
      }
      case "html":
        return /^<br\s*\/?>$/i.test(t.raw.trim()) ? h("br") : htmlText(t.raw);
      case "checkbox":
        return h("input", { type: "checkbox", checked: t.checked, disabled: true, "aria-label": t.checked ? "Done" : "Not done" });
      case "text":
        return t.tokens ? renderInline(t.tokens, inLink) : decodeEntities(t.text);
      default:
        return decodeEntities(t.text ?? t.raw ?? "");
    }
  });
}

function renderBlocks(tokens: Token[], headingLevel: number): VNodeChild[] {
  const blocks: VNodeChild[] = [];
  for (const token of tokens) {
    const t = token as Tokens.Generic;
    switch (t.type) {
      case "space":
      case "def":
        break;
      case "heading": {
        const level = Math.min(headingLevel + t.depth - 1, 6);
        blocks.push(h(`h${level}` as HeadingTag, renderInline(t.tokens)));
        break;
      }
      case "paragraph":
        blocks.push(h("p", renderInline(t.tokens)));
        break;
      case "text":
        blocks.push(h("p", t.tokens ? renderInline(t.tokens) : decodeEntities(t.text)));
        break;
      case "code":
        blocks.push(h("pre", h("code", t.text)));
        break;
      case "blockquote":
        blocks.push(h("blockquote", renderBlocks(t.tokens ?? [], headingLevel)));
        break;
      case "hr":
        blocks.push(h("hr"));
        break;
      case "list": {
        const list = t as Tokens.List;
        const items = list.items.map((item) => h("li", item.task ? { class: "task" } : undefined,
          item.loose ? renderBlocks(item.tokens, headingLevel) : renderListItem(item.tokens, headingLevel)));
        blocks.push(list.ordered
          ? h("ol", list.start !== "" && list.start !== 1 ? { start: list.start } : undefined, items)
          : h("ul", items));
        break;
      }
      case "table": {
        const table = t as Tokens.Table;
        const cell = (tag: "th" | "td", c: Tokens.TableCell) =>
          h(tag, c.align ? { style: { textAlign: c.align } } : undefined, renderInline(c.tokens));
        blocks.push(h("div", { class: "release-markdown-table" }, h("table", [
          h("thead", h("tr", table.header.map((c) => cell("th", c)))),
          h("tbody", table.rows.map((row) => h("tr", row.map((c) => cell("td", c))))),
        ])));
        break;
      }
      case "html": {
        const text = htmlText(t.raw).trim();
        if (text) blocks.push(h("p", text));
        break;
      }
      default:
        blocks.push(h("p", decodeEntities(t.text ?? t.raw ?? "")));
    }
  }
  return blocks;
}

// Tight list items hold bare inline "text" tokens mixed with nested lists.
function renderListItem(tokens: Token[], headingLevel: number): VNodeChild[] {
  return tokens.flatMap((token) => token.type === "text" || token.type === "checkbox"
    ? renderInline([token])
    : renderBlocks([token], headingLevel));
}

export default defineComponent({
  name: "ReleaseNotesMarkdown",
  props: {
    source: { type: String, required: true },
    /** Heading element used for a markdown `#` heading; deeper levels follow. */
    headingLevel: { type: Number as PropType<3 | 4>, default: 3 },
    /** Render single newlines as line breaks, as GitHub does for release bodies. */
    breaks: { type: Boolean, default: false },
  },
  setup(props) {
    return () => {
      let content: VNodeChild[];
      try {
        content = renderBlocks(new Lexer({ gfm: true, breaks: props.breaks }).lex(props.source), props.headingLevel);
      } catch (error) {
        console.warn("WUDup could not format these release notes, so they are shown as plain text.", error);
        content = [h("pre", props.source)];
      }
      return h("div", { class: "release-markdown" }, content);
    };
  },
});
