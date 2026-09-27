import type { PendingCleanupLine } from "../types";

export function cleanupLineKey(line: PendingCleanupLine): string {
  return `${line.line_no}\u0000${line.raw}`;
}

export function clone<T>(value: T): T {
  return structuredClone(value);
}

export function demoIdPart(value: string): string {
  let result = "";
  let needsSeparator = false;
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    const isAllowed =
      (code >= 48 && code <= 57)
      || (code >= 65 && code <= 90)
      || (code >= 97 && code <= 122)
      || char === "_"
      || char === "."
      || char === "-";
    if (isAllowed) {
      if (needsSeparator && result !== "") {
        result += "-";
      }
      result += char;
      needsSeparator = false;
    } else if (result !== "") {
      needsSeparator = true;
    }
  }
  let start = 0;
  let end = result.length;
  while (start < end && result[start] === "-") {
    start += 1;
  }
  while (end > start && result[end - 1] === "-") {
    end -= 1;
  }
  return result.slice(start, end) || "item";
}
