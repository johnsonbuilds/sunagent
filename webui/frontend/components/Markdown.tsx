"use client";

import { render as renderMarkdown } from "@/lib/markdown";

interface MarkdownProps {
  text: string;
  className?: string;
}

/** Renders Markdown as HTML preview, sharing the transcript's `.md-body` styles. */
export default function Markdown({ text, className }: MarkdownProps) {
  return (
    <div
      className={className ? `md-body ${className}` : "md-body"}
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text || "") }}
    />
  );
}
