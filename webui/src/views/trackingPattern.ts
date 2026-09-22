// Mirror the safe subset accepted by the tracking-repair preview. The server
// remains authoritative; never evaluate arbitrary operator regex in the browser.
const tokenPattern = /(?:[A-Za-z0-9_-]|\\\.|\\d\+|\(\?:\\\.\\d\+\)\+)/y;
const dockerTag = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
const dotNumberParts = "(?:\\.\\d+)+";

export interface TrackingPatternGuide {
  description: string;
  summary: string;
  examples: { tag: string; change: string; matches: boolean }[];
  matches: (tag: string) => boolean | null;
}

interface PatternScan {
  parts: string[];
  literal: string;
  hasNumber: boolean;
  unseparatedNumber: boolean;
}

function consumePatternToken(scan: PatternScan, token: string): boolean {
  if (token === "\\d+" || token === dotNumberParts) {
    if (token === "\\d+" && scan.unseparatedNumber) return false;
    if (scan.literal) scan.parts.push("“" + scan.literal + "”");
    scan.literal = "";
    scan.parts.push(token === dotNumberParts ? "one or more dots, each followed by digits" : "one or more digits");
    scan.hasNumber = true;
    scan.unseparatedNumber = true;
  } else {
    scan.literal += token === "\\." ? "." : token;
    if (token === "\\." || !/^\d$/.test(token)) scan.unseparatedNumber = false;
  }
  return true;
}

function patternParts(body: string): { parts: string[]; hasNumber: boolean } | null {
  const scan: PatternScan = { parts: [], literal: "", hasNumber: false, unseparatedNumber: false };
  let offset = 0;

  while (offset < body.length) {
    tokenPattern.lastIndex = offset;
    const token = tokenPattern.exec(body)?.[0];
    if (!token || !consumePatternToken(scan, token)) return null;
    offset = tokenPattern.lastIndex;
  }
  if (!scan.hasNumber && !scan.literal) return null;
  if (scan.literal) scan.parts.push("“" + scan.literal + "”");
  return { parts: scan.parts, hasNumber: scan.hasNumber };
}

// Illustrations only: change the installed tag's structure, then test each
// candidate with the same restricted matcher used for operator input.
function exampleTags(tag: string): { tag: string; change: string }[] {
  if (!dockerTag.test(tag)) return [];
  const examples: { tag: string; change: string }[] = [];
  const version = /^(v?)(\d+(?:\.\d+)+)(.*)$/.exec(tag);
  const increment = (digits: string) => String(BigInt(digits) + 1n);
  if (version) {
    const [, prefix = "", numbers = "", suffix = ""] = version;
    const parts = numbers.split(".");
    const next = [...parts];
    next[next.length - 1] = increment(next[next.length - 1]!);
    examples.push({ tag: prefix + next.join(".") + suffix, change: "Version number change" });
    const first = [increment(parts[0]!), ...parts.slice(1).map(() => "0")];
    examples.push({ tag: prefix + first.join(".") + suffix, change: "First version number change" });
    examples.push({
      tag: prefix + (parts.length === 2 ? numbers + ".1" : parts.slice(0, -1).join(".")) + suffix,
      change: parts.length === 2 ? "With a patch number" : "Fewer version parts",
    });
    if (suffix) {
      const suffixNumbers = Array.from(suffix.matchAll(/\d+/g));
      for (const number of [suffixNumbers[0], suffixNumbers.length > 1 ? suffixNumbers.at(-1) : undefined]) {
        if (!number) continue;
        const changedSuffix = suffix.slice(0, number.index) + increment(number[0]) + suffix.slice(number.index + number[0].length);
        examples.push({ tag: prefix + numbers + changedSuffix, change: "Suffix number change" });
      }
      examples.push({ tag: prefix + numbers, change: "Without the suffix" });
    }
  } else if (/\d/.test(tag)) {
    examples.push({ tag: tag.replace(/\d+/, increment), change: "First number change" });
    const last = tag.replace(/\d+(?!.*\d)/, increment);
    if (last !== examples[0]?.tag) examples.push({ tag: last, change: "Last number change" });
  }
  examples.push({ tag: tag + "-rc1", change: "Extra prerelease suffix" });
  examples.push({ tag: "latest", change: "Floating tag" });
  return examples.filter((example) => example.tag !== tag && dockerTag.test(example.tag));
}

export function explainTrackingPattern(pattern: string, installedTag = ""): TrackingPatternGuide | null {
  if (pattern.length > 256 || !pattern.startsWith("^") || !pattern.endsWith("$")) {
    return null;
  }
  const parsed = patternParts(pattern.slice(1, -1));
  if (!parsed) return null;
  const { parts, hasNumber } = parsed;

  const expression = new RegExp(pattern);
  const pinnedMajor = /^\^v?(\d+)(?:\\\.|\(\?:\\\.)/.exec(pattern)?.[1];
  const versionBody = pattern.slice(1).replace(/^v/, "");
  const majorNote = versionBody.startsWith("\\d+\\.") || versionBody.startsWith("\\d+(?:\\.")
    ? " The first number can change too, including to a new major version."
    : "";
  const description = hasNumber
    ? "Matches an entire tag with " + parts.join(", followed by ") +
      ". Other prefixes or suffixes do not match." + majorNote +
      (pattern === "^\\d+$" ? " This also includes other all-numeric tags." : "")
    : "Matches only the tag " + parts[0] +
      ". It will not follow differently named tags. Detecting changes under this same tag depends on WUD digest watching.";
  return {
    description,
    summary: !hasNumber ? "Tracks this exact tag; same-tag updates depend on WUD digest watching."
      : majorNote ? "Version numbers can change, including to a new major version."
      : pinnedMajor ? `Keeps the first version number at ${pinnedMajor}; fixed prefixes and suffixes must still match.`
      : "Only tags matching the fixed parts and numeric pattern are included.",
    examples: exampleTags(installedTag).map((example) => ({ ...example, matches: expression.test(example.tag) })),
    matches: (tag) => dockerTag.test(tag) ? expression.test(tag) : null,
  };
}
