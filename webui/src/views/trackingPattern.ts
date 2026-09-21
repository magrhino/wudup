// Mirror the safe subset accepted by the tracking-repair preview. The server
// remains authoritative; never evaluate arbitrary operator regex in the browser.
const tokenPattern = /(?:[A-Za-z0-9_-]|\\\.|\\d\+|\(\?:\\\.\\d\+\)\+)/y;
const dockerTag = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
const dotNumberParts = "(?:\\.\\d+)+";

export interface TrackingPatternGuide {
  description: string;
  matches: (tag: string) => boolean | null;
}

export function explainTrackingPattern(pattern: string): TrackingPatternGuide | null {
  if (pattern.length > 256 || !pattern.startsWith("^") || !pattern.endsWith("$")) {
    return null;
  }

  const body = pattern.slice(1, -1);
  const parts: string[] = [];
  let literal = "";
  let offset = 0;
  let hasNumber = false;
  let unseparatedNumber = false;
  const flushLiteral = () => {
    if (literal) parts.push("“" + literal + "”");
    literal = "";
  };

  while (offset < body.length) {
    tokenPattern.lastIndex = offset;
    const token = tokenPattern.exec(body)?.[0];
    if (!token) return null;
    if (token === "\\d+" || token === dotNumberParts) {
      if (token === "\\d+" && unseparatedNumber) return null;
      flushLiteral();
      parts.push(token === dotNumberParts ? "one or more dots, each followed by digits" : "one or more digits");
      hasNumber = true;
      unseparatedNumber = true;
    } else {
      literal += token === "\\." ? "." : token;
      if (token === "\\." || !/^\d$/.test(token)) unseparatedNumber = false;
    }
    offset = tokenPattern.lastIndex;
  }
  if (!hasNumber && !literal) return null;
  flushLiteral();

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
