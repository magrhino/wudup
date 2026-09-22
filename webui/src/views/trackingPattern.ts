// Mirror the safe subset accepted by the tracking-repair preview. The server
// remains authoritative; never evaluate arbitrary operator regex in the browser.
const tokenPattern = /(?:[A-Za-z0-9_-]|\\\.|\\d\+|\(\?:\\\.\\d\+\)\+)/y;
const dockerTag = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
const dotNumberParts = "(?:\\.\\d+)+";

export interface TrackingPatternGuide {
  description: string;
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

export function explainTrackingPattern(pattern: string): TrackingPatternGuide | null {
  if (pattern.length > 256 || !pattern.startsWith("^") || !pattern.endsWith("$")) {
    return null;
  }
  const parsed = patternParts(pattern.slice(1, -1));
  if (!parsed) return null;
  const { parts, hasNumber } = parsed;

  const expression = new RegExp(pattern);
  const majorNote = pattern === "^v\\d+(?:\\.\\d+)+$" ||
    pattern === "^\\d+(?:\\.\\d+)+$"
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
    matches: (tag) => dockerTag.test(tag) ? expression.test(tag) : null,
  };
}
