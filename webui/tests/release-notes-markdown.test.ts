import { mount } from "@vue/test-utils";
import { Lexer } from "marked";
import { describe, expect, it, vi } from "vitest";
import ReleaseNotesMarkdown from "../src/components/pending/ReleaseNotesMarkdown";

function render(source: string, headingLevel?: 3 | 4, breaks?: boolean) {
  return mount(ReleaseNotesMarkdown, { props: { source, headingLevel, breaks } });
}

describe("release notes markdown", () => {
  it("never renders upstream HTML, scripts, or images", () => {
    const wrapper = render([
      '<script>alert(1)</script><img src="https://tracker.example/p.gif" onerror="alert(2)">',
      "",
      '<details><summary>More</summary>Hidden &amp; shown</details>',
      "",
      "![badge](https://img.example/badge.svg) Text with <b>bold</b> tag",
    ].join("\n"));
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find("details").exists()).toBe(false);
    expect(wrapper.find("b").exists()).toBe(false);
    expect(wrapper.html()).not.toMatch(/onerror|<script/);
    expect(wrapper.text()).toContain("MoreHidden & shown");
    expect(wrapper.find('a[href="https://img.example/badge.svg"]').text()).toBe("[badge]");
  });

  it("keeps only absolute http(s) links clickable", () => {
    const wrapper = render("[bad](javascript:alert(1)) [rel](../compare) [creds](https://u:p@example.com) [ok](https://example.com/a)");
    const links = wrapper.findAll("a");
    expect(links.map((link) => link.attributes("href"))).toEqual(["https://example.com/a"]);
    expect(links[0].attributes("target")).toBe("_blank");
    expect(wrapper.text()).toContain("bad rel creds ok");
  });

  it("renders GFM structure used by release notes", () => {
    const wrapper = render([
      "# Title",
      "1. first",
      "   - nested `code`",
      "2. ~~second~~",
      "",
      "- [x] done",
      "- [ ] todo",
      "",
      "> quoted",
      "",
      "```sh",
      "docker pull a && b",
      "```",
      "",
      "| Key | Value |",
      "| --- | :---: |",
      "| a | 1 &lt; 2 |",
      "",
      "---",
    ].join("\n"), 4);
    expect(wrapper.find("h4").text()).toBe("Title");
    expect(wrapper.find("ol > li > ul > li code").text()).toBe("code");
    expect(wrapper.find("ol del").text()).toBe("second");
    const boxes = wrapper.findAll('input[type="checkbox"]');
    expect(boxes.map((box) => (box.element as HTMLInputElement).checked)).toEqual([true, false]);
    expect(boxes.every((box) => box.attributes("disabled") !== undefined)).toBe(true);
    expect(wrapper.find("blockquote").text()).toBe("quoted");
    expect(wrapper.find("pre code").text()).toBe("docker pull a && b");
    expect(wrapper.find("td[style]").text()).toBe("1 < 2");
    expect(wrapper.find("hr").exists()).toBe(true);
  });

  it("keeps single newlines only when release-body line breaks are requested", () => {
    expect(render("Fixed X\nFixed Y", 3, true).findAll("p br")).toHaveLength(1);
    expect(render("Fixed X\nFixed Y").find("br").exists()).toBe(false);
  });

  it("decodes entities once, leaves code and autolinks literal, and resolves link destinations", () => {
    const wrapper = render("&amp;lt; `&lt;T&gt;` [q](https://e.example/?a=1&amp;b=2) https://e.example/?c&amp;d [x](java&#115;cript:alert(1)) ![**bold** alt](https://e.example/i.png)");
    const [text] = wrapper.find("p").element.childNodes;
    expect(text.textContent).toBe("&lt; ");
    expect(wrapper.find("code").text()).toBe("&lt;T&gt;");
    expect(wrapper.findAll("a").map((a) => a.attributes("href"))).toEqual([
      "https://e.example/?a=1&b=2",
      "https://e.example/?c&amp;d",
      "https://e.example/i.png",
    ]);
    expect(wrapper.findAll("a")[1].text()).toBe("https://e.example/?c&amp;d");
    expect(wrapper.text()).toContain("x [bold alt]");
    expect(wrapper.find("a strong").text()).toBe("bold");
  });

  it("decodes common named entities and leaves unknown names literal", () => {
    expect(render("v1 &mdash; fixes &hellip; &copy; &rarr; &unknown;").text()).toBe("v1 \u2014 fixes \u2026 \u00a9 \u2192 &unknown;");
  });

  it("warns and shows plain text when markdown cannot be formatted", () => {
    const failure = new Error("lexer failed");
    vi.spyOn(Lexer.prototype, "lex").mockImplementationOnce(() => { throw failure; });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const wrapper = render("**raw** notes");
    expect(wrapper.find("pre").text()).toBe("**raw** notes");
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("shown as plain text"), failure);
    warn.mockRestore();
  });
});
