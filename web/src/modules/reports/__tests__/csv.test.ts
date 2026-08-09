/**
 * Strict CSV parser (U2 on-screen view) — pure-function matrix with
 * hand-written oracles. The grammar under test is exactly what the
 * backend CsvBuilder emits (Python csv module: CRLF terminators,
 * minimal quoting, "" escapes) plus LF tolerance; EVERYTHING else
 * throws — a malformed artifact must never render as silently
 * mis-split figures on a financial surface.
 */
import { CsvParseError, parseCsv } from "../csv";

describe("parseCsv — the backend CsvBuilder grammar", () => {
  it("parses CRLF records with minimal quoting verbatim", () => {
    expect(parseCsv("Member No,Amount\r\nGP-001,150.00\r\nGP-002,0.00\r\n")).toEqual([
      ["Member No", "Amount"],
      ["GP-001", "150.00"],
      ["GP-002", "0.00"],
    ]);
  });

  it("keeps a quoted grouped figure as ONE cell (embedded comma)", () => {
    expect(parseCsv('A,B\r\nGP-001,"1,234.56"\r\n')).toEqual([
      ["A", "B"],
      ["GP-001", "1,234.56"],
    ]);
  });

  it("unescapes doubled quotes and preserves embedded newlines inside quoted cells", () => {
    expect(parseCsv('A\r\n"say ""hi""\r\nline two"\r\n')).toEqual([
      ["A"],
      ['say "hi"\r\nline two'],
    ]);
  });

  it("accepts bare-LF terminators (robustness) and a missing trailing terminator", () => {
    expect(parseCsv("A,B\nx,y")).toEqual([
      ["A", "B"],
      ["x", "y"],
    ]);
  });

  it("preserves the server's OWASP formula-escape prefix VERBATIM (never re-interpreted)", () => {
    expect(parseCsv("A\r\n'=SUM(A1:A9)\r\n")).toEqual([["A"], ["'=SUM(A1:A9)"]]);
    expect(parseCsv("A\r\n'-150.00\r\n")).toEqual([["A"], ["'-150.00"]]);
  });

  it("parses empty cells and a quoted empty cell", () => {
    expect(parseCsv('a,,c\r\n"",y,\r\n')).toEqual([
      ["a", "", "c"],
      ["", "y", ""],
    ]);
  });

  it("returns [] for an empty document (no vacuous header row)", () => {
    expect(parseCsv("")).toEqual([]);
  });

  it("THROWS on an unterminated quoted cell", () => {
    expect(() => parseCsv('A\r\n"unterminated')).toThrow(CsvParseError);
  });

  it("THROWS on characters after a closing quote (a mis-split figure risk)", () => {
    expect(() => parseCsv('A\r\n"150.00"7,x\r\n')).toThrow(CsvParseError);
  });

  it("THROWS on a stray quote inside an unquoted cell", () => {
    expect(() => parseCsv('A\r\n15"0.00\r\n')).toThrow(CsvParseError);
  });

  it("THROWS on a bare carriage return outside a quoted cell", () => {
    expect(() => parseCsv("A\rB\r\n")).toThrow(CsvParseError);
  });

  it("hostile markup survives as verbatim TEXT cells (inertness is the renderer's job, fidelity is ours)", () => {
    // The payload carries quotes, so the WIRE cell must be quoted with
    // doubled quotes (RFC 4180 — the stray-quote leg above proves the
    // unquoted form is rightly rejected); the PARSED value is the
    // hostile string byte-identically.
    const hostile = '<img src=x onerror="steal()">';
    const wireCell = `"${hostile.replaceAll('"', '""')}"`;
    expect(parseCsv(`A\r\n${wireCell}\r\n`)).toEqual([["A"], [hostile]]);
  });
});
