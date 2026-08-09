/**
 * Strict CSV parsing for the ON-SCREEN report view (pure string processing, no DOM, no I/O).
 *
 * The input is the server's OWN rendered artifact: the backend
 * CsvBuilder (backend/src/genesis/domain/documents.py) writes with
 * Python's csv module — CRLF record terminator, minimal quoting,
 * double-quote ("") escapes — i.e. RFC 4180. This parser accepts
 * exactly that grammar (LF accepted as a terminator for robustness)
 * and THROWS on anything else (an unterminated quote, a stray quote
 * inside an unquoted field, characters after a closing quote): a
 * malformed artifact must surface as an honest error, never as
 * silently mis-split FIGURES on a financial surface.
 *
 * Cells are returned VERBATIM — including the server's own OWASP
 * formula-escape prefix (a leading ' on text cells that would
 * otherwise start with = + - @). Nothing is summed, derived, coerced
 * or re-formatted here; the caller renders cells as inert React text.
 */

export class CsvParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CsvParseError";
  }
}

/** Parse an RFC 4180 CSV document into rows of verbatim cell strings. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQuotes = false;
  /** True once the current cell was opened (or closed) as a quoted
   * cell — a bare quote later in an unquoted cell is malformed. */
  let cellWasQuoted = false;
  let index = 0;

  function endCell(): void {
    row.push(cell);
    cell = "";
    cellWasQuoted = false;
  }

  function endRow(): void {
    endCell();
    rows.push(row);
    row = [];
  }

  while (index < text.length) {
    const char = text[index];
    if (inQuotes) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          cell += '"';
          index += 2;
          continue;
        }
        inQuotes = false;
        index += 1;
        // After a closing quote only a separator/terminator/EOF may
        // follow — anything else is a malformed artifact.
        const next = text[index];
        if (next !== undefined && next !== "," && next !== "\r" && next !== "\n") {
          throw new CsvParseError("unexpected character after a closing quote");
        }
        continue;
      }
      cell += char;
      index += 1;
      continue;
    }
    if (char === '"') {
      if (cell !== "" || cellWasQuoted) {
        throw new CsvParseError("stray quote inside an unquoted cell");
      }
      inQuotes = true;
      cellWasQuoted = true;
      index += 1;
      continue;
    }
    if (char === ",") {
      endCell();
      index += 1;
      continue;
    }
    if (char === "\r") {
      if (text[index + 1] !== "\n") {
        throw new CsvParseError("bare carriage return outside a quoted cell");
      }
      endRow();
      index += 2;
      continue;
    }
    if (char === "\n") {
      endRow();
      index += 1;
      continue;
    }
    cell += char;
    index += 1;
  }

  if (inQuotes) {
    throw new CsvParseError("unterminated quoted cell");
  }
  // A trailing record terminator (the writer always emits one) leaves
  // nothing pending; any pending content is the final record.
  if (cell !== "" || cellWasQuoted || row.length > 0) {
    endRow();
  }
  return rows;
}
