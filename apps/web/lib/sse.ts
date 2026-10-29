// Minimal Server-Sent Events parser for a fetch() body stream.
//
// Browser EventSource cannot send an Authorization header, so the live feed is read with
// fetch() + ReadableStream instead and parsed here (https://html.spec.whatwg.org/#event-stream-interpretation).

export interface SseMessage {
  /** "message" when the server sent no event: field. */
  event: string;
  data: string;
  /** Last event ID in effect for this message (the reconnect cursor), if any. */
  id: string | null;
}

export interface SseCallbacks {
  onMessage: (m: SseMessage) => void;
  onComment?: (text: string) => void;
  onRetry?: (ms: number) => void;
}

/** Incremental parser: feed it decoded text chunks in order. */
export class SseParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];
  private lastId: string | null = null;

  constructor(private cb: SseCallbacks) {}

  push(chunk: string): void {
    this.buffer += chunk;
    // Lines end in CRLF, LF or CR; keep a trailing partial line in the buffer. A trailing CR is
    // held back too: its LF may arrive in the next chunk, and CR+LF is one line ending.
    let text = this.buffer;
    const heldCr = text.endsWith("\r");
    if (heldCr) text = text.slice(0, -1);
    const lines = text.split(/\r\n|\r|\n/);
    this.buffer = (lines.pop() ?? "") + (heldCr ? "\r" : "");
    for (const line of lines) this.line(line);
  }

  private line(line: string): void {
    if (line === "") {
      this.dispatch();
      return;
    }
    if (line.startsWith(":")) {
      this.cb.onComment?.(line.slice(1).trim());
      return;
    }
    const i = line.indexOf(":");
    const field = i === -1 ? line : line.slice(0, i);
    let value = i === -1 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "event":
        this.event = value;
        break;
      case "data":
        this.data.push(value);
        break;
      case "id":
        if (!value.includes("\0")) this.lastId = value;
        break;
      case "retry":
        if (/^\d+$/.test(value)) this.cb.onRetry?.(Number(value));
        break;
    }
  }

  private dispatch(): void {
    if (this.data.length === 0) {
      this.event = "";
      return;
    }
    this.cb.onMessage({ event: this.event || "message", data: this.data.join("\n"), id: this.lastId });
    this.event = "";
    this.data = [];
  }
}
