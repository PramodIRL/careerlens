"use client";

import type { ReactNode } from "react";

/** A small Markdown renderer for assistant replies.
 *
 * SAFE BY CONSTRUCTION: it builds React elements from the text and never
 * sets HTML, so a `<script>` in a reply (or in a job description the
 * model quoted) renders as literal text. Links are kept only for http(s)
 * URLs and open in a new tab without an opener.
 *
 * Covers what chat replies actually use: headings, paragraphs, bullet
 * and numbered lists, fenced code, and inline bold, italic, code and
 * links. Anything else falls through as plain text. */

const INLINE =
  /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\)|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(INLINE)) {
    const token = match[0];
    const start = match.index ?? 0;
    if (start > last) nodes.push(text.slice(last, start));
    const key = `${keyPrefix}-${index++}`;
    if (token.startsWith("**")) {
      nodes.push(
        <strong key={key} className="font-semibold text-foreground">
          {token.slice(2, -2)}
        </strong>,
      );
    } else if (token.startsWith("`")) {
      nodes.push(
        <code
          key={key}
          className="rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-[0.85em]"
        >
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("[")) {
      const split = token.indexOf("](");
      const label = token.slice(1, split);
      const href = token.slice(split + 2, -1);
      nodes.push(
        /^https?:\/\//i.test(href) ? (
          <a
            key={key}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="font-medium text-accent underline underline-offset-2 hover:opacity-80"
          >
            {label}
          </a>
        ) : (
          label
        ),
      );
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    }
    last = start + token.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "code"; text: string };

function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        body.push(lines[i++]);
      }
      i++;
      blocks.push({ kind: "code", text: body.join("\n") });
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: heading[1].length,
        text: heading[2],
      });
      i++;
      continue;
    }
    const bullet = /^\s*[-*+]\s+(.*)$/;
    const numbered = /^\s*\d+[.)]\s+(.*)$/;
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const pattern = ordered ? numbered : bullet;
      const items: string[] = [];
      while (i < lines.length && pattern.test(lines[i])) {
        items.push((pattern.exec(lines[i]) as RegExpExecArray)[1]);
        i++;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const paragraph: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,4})\s/.test(lines[i]) &&
      !bullet.test(lines[i]) &&
      !numbered.test(lines[i]) &&
      !lines[i].trim().startsWith("```")
    ) {
      paragraph.push(lines[i++].trim());
    }
    blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
  }
  return blocks;
}

export default function Markdown({ source }: { source: string }) {
  return (
    <div className="space-y-2.5 text-sm leading-relaxed">
      {parseBlocks(source).map((block, b) => {
        const key = `b${b}`;
        switch (block.kind) {
          case "heading":
            return (
              <p
                key={key}
                className={
                  block.level <= 2
                    ? "pt-1 text-[0.95rem] font-semibold tracking-tight text-foreground"
                    : "pt-1 text-sm font-semibold text-foreground"
                }
              >
                {renderInline(block.text, key)}
              </p>
            );
          case "list": {
            const ListTag = block.ordered ? "ol" : "ul";
            return (
              <ListTag
                key={key}
                className={`space-y-1 pl-5 ${block.ordered ? "list-decimal" : "list-disc"} marker:text-muted`}
              >
                {block.items.map((item, n) => (
                  <li key={`${key}-${n}`}>
                    {renderInline(item, `${key}-${n}`)}
                  </li>
                ))}
              </ListTag>
            );
          }
          case "code":
            return (
              <pre
                key={key}
                className="overflow-x-auto rounded-xl border border-border bg-surface-2 p-3 font-mono text-xs"
              >
                <code>{block.text}</code>
              </pre>
            );
          default:
            return <p key={key}>{renderInline(block.text, key)}</p>;
        }
      })}
    </div>
  );
}
